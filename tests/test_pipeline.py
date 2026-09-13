"""Regression tests for the deterministic parts of the pipeline: JSON repair,
repetition-collapse detection, the charge-vs-tax and withholding-sign safety
nets in assemble.py, and master-data matching. These run entirely offline
(no LLM calls) - they exist because until now this logic was only ever
exercised by hand, one document at a time, during the actual take-home run.

    python -m pytest tests/ -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.assemble import _assemble_tax, _is_charge_not_tax, assemble_payable
from src.hive_client import _extract_json, _find_matching_brace, _is_degenerate, _repair_json
from src.master_match import MasterData


# ---------------------------------------------------------------------------
# JSON repair (src/hive_client.py)
# ---------------------------------------------------------------------------

def test_extract_json_plain():
    assert _extract_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_extract_json_fenced_code_block():
    text = '```json\n{"a": 1}\n```'
    assert _extract_json(text) == {"a": 1}


def test_extract_json_truncated_but_otherwise_complete_is_closed_without_loss():
    # Ran out of tokens right after the last real value - closing brackets
    # should recover every field, not just some of them.
    truncated = '{"payables": [{"invoice_number": "123", "gross_total": "45.00"'
    result = _extract_json(truncated)
    assert result["payables"][0]["invoice_number"] == "123"
    assert result["payables"][0]["gross_total"] == "45.00"


def test_extract_json_does_not_pick_an_earlier_brace_on_truncation():
    # This is the exact bug that shipped once: naive `rfind("}")` finds an
    # EARLY, structurally-valid-looking brace instead of the true (missing)
    # end, silently dropping every field declared after it.
    truncated = (
        '{"payables": [{"invoice_number": "123"}], '
        '"declined": [{"doc_type": "x", "reason": "still writing this par'
    )
    result = _extract_json(truncated)
    # The declined entry got cut mid-string; repair must not silently
    # resurrect a fake empty top-level object from the first "}" it can see.
    assert result["payables"][0]["invoice_number"] == "123"


def test_extract_json_mid_value_truncation_drops_only_the_cut_field():
    # A value cut off mid-flight (e.g. repetition-collapse slipping past
    # detection) - the field being written when it died is lost, but every
    # earlier field must survive.
    truncated = '{"a": "1", "b": "2", "c": "unterminated string that never clo'
    repaired = _repair_json(truncated)
    parsed = json.loads(repaired, strict=False)
    assert parsed["a"] == "1"
    assert parsed["b"] == "2"


def test_find_matching_brace_ignores_braces_inside_strings():
    text = '{"description": "a {weird} value"}'
    end = _find_matching_brace(text, 0)
    assert end == len(text) - 1


def test_find_matching_brace_returns_none_when_never_closed():
    assert _find_matching_brace('{"a": {"b": 1}', 0) is None


# ---------------------------------------------------------------------------
# Repetition-collapse detection (src/hive_client.py)
# ---------------------------------------------------------------------------

def test_is_degenerate_detects_repeated_tail_chunk():
    text = "some normal preamble. " + ("do de fornecimento. " * 20)
    assert _is_degenerate(text) is True


def test_is_degenerate_false_for_normal_json_output():
    text = json.dumps({"payables": [{"description": f"line {i}"} for i in range(20)]})
    assert _is_degenerate(text) is False


def test_is_degenerate_false_for_short_text():
    assert _is_degenerate("short") is False


# ---------------------------------------------------------------------------
# Charge-vs-tax reclassification (src/assemble.py)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tax_type,tax_name",
    [
        ("TAX", "Fuel Surcharge"),
        ("", "Handling Fee"),
        ("TAX", "Delivery Fee"),
        ("", "Service Charge"),
    ],
)
def test_is_charge_not_tax_flags_disguised_charges(tax_type, tax_name):
    assert _is_charge_not_tax(tax_type, tax_name) is True


@pytest.mark.parametrize(
    "tax_type,tax_name",
    [
        ("VAT", "VAT"),
        ("GST", "GST on sales"),
        ("", "Excise Duty"),
        ("WHT", "Withholding Tax"),
    ],
)
def test_is_charge_not_tax_leaves_real_taxes_alone(tax_type, tax_name):
    assert _is_charge_not_tax(tax_type, tax_name) is False


def test_assemble_payable_moves_disguised_charge_into_extra_charges():
    master = MasterData()
    raw = {
        "currency": "EUR",
        "gross_total": "110.00",
        "extra_charges": "",
        "taxes": [{"tax_type": "TAX", "tax_name": "Fuel Surcharge", "tax_rate": "", "tax_amount": "10.00"}],
        "line_items": [],
    }
    result = assemble_payable(raw, master)
    assert result["taxes"] == []
    assert result["extra_charges"] == "10"


def test_assemble_payable_keeps_real_header_tax():
    master = MasterData()
    raw = {
        "currency": "EUR",
        "gross_total": "123.00",
        "taxes": [{"tax_type": "VAT", "tax_name": "VAT", "tax_rate": "23", "tax_amount": "23.00"}],
        "line_items": [],
    }
    result = assemble_payable(raw, master)
    assert len(result["taxes"]) == 1
    assert result["taxes"][0]["tax_amount"] == "23"


# ---------------------------------------------------------------------------
# Withholding-tax sign normalisation (src/assemble.py)
# ---------------------------------------------------------------------------

def test_withholding_tax_amount_normalised_to_negative():
    master = MasterData()
    t = _assemble_tax({"tax_type": "WHT", "tax_name": "Withholding Tax", "tax_rate": "5", "tax_amount": "50.00"}, master)
    assert t["tax_amount"] == "-50"


def test_withholding_tax_already_negative_is_untouched():
    master = MasterData()
    t = _assemble_tax({"tax_type": "WHT", "tax_name": "Withholding Tax", "tax_rate": "5", "tax_amount": "-50.00"}, master)
    assert t["tax_amount"] == "-50"


def test_ordinary_tax_sign_is_untouched():
    master = MasterData()
    t = _assemble_tax({"tax_type": "VAT", "tax_name": "VAT", "tax_rate": "20", "tax_amount": "20.00"}, master)
    assert t["tax_amount"] == "20"


# ---------------------------------------------------------------------------
# Master-data matching (src/master_match.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def master():
    return MasterData()


def test_supplier_exact_vat_match(master):
    supplier_id = master.match_supplier("Some Slightly Different Name GmbH", "DE209177122")
    assert supplier_id == "2845695"


def test_supplier_fuzzy_name_match_above_threshold(master):
    # Close misspelling of a real supplier name, no VAT id given.
    supplier_id = master.match_supplier("Phocus Direct Comunication GmbH", "")
    assert supplier_id == "2845695"


def test_supplier_no_match_returns_blank_not_a_guess(master):
    supplier_id = master.match_supplier("Completely Unrelated Company Ltd", "")
    assert supplier_id == ""


def test_po_exact_match_only(master):
    assert master.match_po("NO-SUCH-PO-NUMBER-XYZ") == ""


def test_tax_no_match_when_country_unknown_and_rate_wrong(master):
    assert master.match_tax("Made Up Tax", "999", "MADEUP", "") == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
