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
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.assemble import _assemble_tax, _is_charge_not_tax, assemble_payable
from src.hive_client import (
    MalformedJSONError,
    _extract_json,
    _find_matching_brace,
    _is_degenerate,
    _repair_json,
    _unclosed_bracket_stack,
)
from src.master_match import MasterData


# ---------------------------------------------------------------------------
# JSON repair (src/hive_client.py)
# ---------------------------------------------------------------------------

def test_extract_json_plain():
    obj, repaired = _extract_json('{"a": 1, "b": "x"}')
    assert obj == {"a": 1, "b": "x"}
    assert repaired is False


def test_extract_json_fenced_code_block():
    text = '```json\n{"a": 1}\n```'
    obj, repaired = _extract_json(text)
    assert obj == {"a": 1}
    assert repaired is False


def test_extract_json_truncated_but_otherwise_complete_is_closed_without_loss():
    # Ran out of tokens right after the last real value - closing brackets
    # should recover every field, not just some of them.
    truncated = '{"payables": [{"invoice_number": "123", "gross_total": "45.00"'
    result, repaired = _extract_json(truncated)
    assert result["payables"][0]["invoice_number"] == "123"
    assert result["payables"][0]["gross_total"] == "45.00"
    assert repaired is True


def test_extract_json_does_not_pick_an_earlier_brace_on_truncation():
    # This is the exact bug that shipped once: naive `rfind("}")` finds an
    # EARLY, structurally-valid-looking brace instead of the true (missing)
    # end, silently dropping every field declared after it.
    truncated = (
        '{"payables": [{"invoice_number": "123"}], '
        '"declined": [{"doc_type": "x", "reason": "still writing this par'
    )
    result, repaired = _extract_json(truncated)
    # The declined entry got cut mid-string; repair must not silently
    # resurrect a fake empty top-level object from the first "}" it can see.
    assert result["payables"][0]["invoice_number"] == "123"
    assert repaired is True


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
# Malformed (not just truncated) bracket structure must be REJECTED, not
# silently "repaired" into something that merely happens to parse.
# ---------------------------------------------------------------------------

def test_unclosed_bracket_stack_rejects_mismatched_closer():
    # ']' appears where the innermost open bracket is '{', not '['.
    with pytest.raises(MalformedJSONError):
        _unclosed_bracket_stack('{"a": ]')


def test_unclosed_bracket_stack_rejects_closer_with_nothing_open():
    with pytest.raises(MalformedJSONError):
        _unclosed_bracket_stack('{"a": 1}]')


def test_unclosed_bracket_stack_ignores_brackets_inside_strings():
    # A literal '[' and ']' inside a string value must not affect the stack.
    # Both the outer '{' and the trailing '[' are genuinely still open here.
    stack = _unclosed_bracket_stack('{"a": "list-like [1, 2] text", "b": [1')
    assert stack == ["{", "["]


def test_unclosed_bracket_stack_handles_escaped_quotes_before_brackets():
    # An escaped quote inside the string must not end the string early and
    # expose the ']' that follows (still inside the string) to the stack.
    s = '{"a": "quote \\" then ] bracket", "b": [1'
    stack = _unclosed_bracket_stack(s)
    assert stack == ["{", "["]


def test_extract_json_raises_on_malformed_nesting_instead_of_repairing():
    with pytest.raises(MalformedJSONError):
        _extract_json('{"payables": [{"a": ]}')


def test_repair_json_raises_on_malformed_nesting():
    with pytest.raises(MalformedJSONError):
        _repair_json('{"a": ]')


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
# Decimal, not float, for assemble.py's internal arithmetic
# ---------------------------------------------------------------------------

def test_charge_reclassification_sums_with_decimal_not_float():
    # float(0.10) + float(0.20) + float(0.30) == 0.6000000000000001 in binary
    # floating point; summing several reclassified charges this way would
    # drift a payable's extra_charges by a fraction of a cent. Decimal must
    # produce exactly "0.6".
    master = MasterData()
    raw = {
        "currency": "EUR",
        "gross_total": "1.00",
        "extra_charges": "",
        "taxes": [
            {"tax_type": "TAX", "tax_name": "Fuel Surcharge", "tax_rate": "", "tax_amount": "0.10"},
            {"tax_type": "TAX", "tax_name": "Handling Fee", "tax_rate": "", "tax_amount": "0.20"},
            {"tax_type": "TAX", "tax_name": "Service Charge", "tax_rate": "", "tax_amount": "0.30"},
        ],
        "line_items": [],
    }
    result = assemble_payable(raw, master)
    assert result["extra_charges"] == "0.6"


def test_charge_reclassification_adds_to_existing_extra_charges_exactly():
    master = MasterData()
    raw = {
        "currency": "EUR",
        "gross_total": "1.00",
        "extra_charges": "10.10",
        "taxes": [{"tax_type": "TAX", "tax_name": "Fuel Surcharge", "tax_rate": "", "tax_amount": "0.20"}],
        "line_items": [],
    }
    result = assemble_payable(raw, master)
    assert result["extra_charges"] == "10.3"


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


# ---------------------------------------------------------------------------
# Retry must never regress an already-valid payable (run.py)
# ---------------------------------------------------------------------------

import run  # noqa: E402 - after sys.path setup above

from src import verify  # noqa: E402


def _footing_payable(invno: str, amount: str) -> dict:
    return {
        "invoice_number": invno,
        "currency": "USD",
        "gross_total": amount,
        "line_items": [{"quantity": "1", "unit_price": amount}],
    }


def test_payable_keys_falls_back_to_index_on_blank_or_duplicate_invoice_number():
    raw = [{"invoice_number": ""}, {"invoice_number": ""}, {"invoice_number": "X"}]
    assert run._payable_keys(raw) == ["__idx0__", "__idx1__", "X"]


def test_reconcile_retry_keeps_valid_payable_the_retry_drops_entirely():
    prev_raw = [_footing_payable("A", "100.00"), _footing_payable("B", "999.00")]
    prev_raw[1]["gross_total"] = "50.00"  # B doesn't foot yet
    prev_assembled = [dict(p) for p in prev_raw]

    # Retry "fixes" B but silently drops A altogether.
    new_raw = [_footing_payable("B", "50.00")]
    new_assembled = [dict(p) for p in new_raw]

    merged_raw, merged_assembled, n_regressed = run._reconcile_retry(
        prev_raw, prev_assembled, new_raw, new_assembled
    )
    assert n_regressed == 1
    keys = {p["invoice_number"] for p in merged_raw}
    assert keys == {"A", "B"}
    for p in merged_assembled:
        ok, booked, stated = verify.check(p)
        assert ok, f"{p['invoice_number']}: booked {booked} != stated {stated}"


def test_reconcile_retry_rejects_a_valid_payable_turned_invalid():
    prev_raw = [_footing_payable("A", "100.00")]
    prev_assembled = [dict(p) for p in prev_raw]

    # Retry returns the same invoice_number but now with a broken total.
    new_raw = [_footing_payable("A", "100.00")]
    new_raw[0]["line_items"][0]["unit_price"] = "1.00"  # now books 1.00, not 100.00
    new_assembled = [dict(p) for p in new_raw]

    merged_raw, merged_assembled, n_regressed = run._reconcile_retry(
        prev_raw, prev_assembled, new_raw, new_assembled
    )
    assert n_regressed == 1
    assert merged_assembled[0]["line_items"][0]["unit_price"] == "100.00"


def test_reconcile_retry_accepts_improvement_on_a_previously_bad_payable():
    prev_raw = [_footing_payable("A", "50.00")]
    prev_raw[0]["gross_total"] = "999.00"  # doesn't foot yet
    prev_assembled = [dict(p) for p in prev_raw]

    new_raw = [_footing_payable("A", "999.00")]  # retry fixed it
    new_assembled = [dict(p) for p in new_raw]

    merged_raw, merged_assembled, n_regressed = run._reconcile_retry(
        prev_raw, prev_assembled, new_raw, new_assembled
    )
    assert n_regressed == 0
    ok, booked, stated = verify.check(merged_assembled[0])
    assert ok


def test_process_file_retry_does_not_drop_a_previously_valid_payable():
    from src.hive_client import UsageTotals
    from src.master_match import MasterData

    raw1 = {
        "payables": [
            _footing_payable("A", "100.00"),
            {**_footing_payable("B", "50.00"), "line_items": [{"quantity": "1", "unit_price": "999.00"}]},
        ],
        "declined": [],
    }
    # Retry fixes B but silently drops A - the exact regression this must catch.
    raw_retry = {"payables": [_footing_payable("B", "50.00")], "declined": []}

    master = MasterData()
    usage = UsageTotals()

    with patch("run.render_pdf_pages", return_value=["data:image/png;base64,AAAA"]), patch(
        "run.call_vision_json", side_effect=[(raw1, False), (raw_retry, False)]
    ):
        result = run.process_file(Path("dummy.pdf"), master, usage)

    invnos = {p["invoice_number"] for p in result["payables"]}
    assert invnos == {"A", "B"}
    for p in result["payables"]:
        ok, booked, stated = verify.check(p)
        assert ok, f"{p['invoice_number']}: booked {booked} != stated {stated}"


def test_process_file_retry_does_not_accept_valid_payable_turned_decline():
    from src.hive_client import UsageTotals
    from src.master_match import MasterData

    raw1 = {
        "payables": [
            _footing_payable("A", "100.00"),
            {**_footing_payable("B", "50.00"), "line_items": [{"quantity": "1", "unit_price": "999.00"}]},
        ],
        "declined": [],
    }
    # Retry fixes B, but reclassifies A as a decline instead of a payable.
    raw_retry = {
        "payables": [_footing_payable("B", "50.00")],
        "declined": [{"doc_type": "Estimate", "reason": "reclassified on second look"}],
    }

    master = MasterData()
    usage = UsageTotals()

    with patch("run.render_pdf_pages", return_value=["data:image/png;base64,AAAA"]), patch(
        "run.call_vision_json", side_effect=[(raw1, False), (raw_retry, False)]
    ):
        result = run.process_file(Path("dummy.pdf"), master, usage)

    invnos = {p["invoice_number"] for p in result["payables"]}
    assert "A" in invnos, "a previously-valid payable must not be silently reclassified as a decline"


def test_process_file_marks_repaired_output_for_review():
    from src.hive_client import UsageTotals
    from src.master_match import MasterData

    raw1 = {"payables": [_footing_payable("A", "100.00")], "declined": []}
    master = MasterData()
    usage = UsageTotals()

    with patch("run.render_pdf_pages", return_value=["data:image/png;base64,AAAA"]), patch(
        "run.call_vision_json", return_value=(raw1, True)  # repaired=True
    ):
        result = run.process_file(Path("dummy.pdf"), master, usage)

    assert result.get("_needs_review") == "json_repaired"


def test_process_file_clean_response_is_not_flagged():
    from src.hive_client import UsageTotals
    from src.master_match import MasterData

    raw1 = {"payables": [_footing_payable("A", "100.00")], "declined": []}
    master = MasterData()
    usage = UsageTotals()

    with patch("run.render_pdf_pages", return_value=["data:image/png;base64,AAAA"]), patch(
        "run.call_vision_json", return_value=(raw1, False)
    ):
        result = run.process_file(Path("dummy.pdf"), master, usage)

    assert "_needs_review" not in result


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
