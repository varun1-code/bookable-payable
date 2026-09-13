#!/usr/bin/env python3
"""The one command: turn every PDF in documents/ into output/<name>.json.

    python run.py                       # process every PDF in documents/
    python run.py --limit 3             # smoke-test on the first 3 files
    python run.py --only INV-01         # process a single file (by stem)
    python run.py --in other_docs --out other_output

See README.md for setup. Needs HIVE_API_KEY (or another OpenAI-compatible
vision API key) in a .env file at the project root - see .env.example.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import config, verify
from src.assemble import assemble_payable
from src.hive_client import UsageTotals, call_vision_json
from src.master_match import MasterData
from src.prompt import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from src.render import render_pdf_pages

RETRY_FEEDBACK_TEMPLATE = """Your previous JSON reply for this same file is below. When I fed
payable index {idx} (invoice_number={invno!r}) into the ERP recompute using exactly the
components you supplied, it booked {erp_gross} {currency}, but your own gross_total for that
payable was {stated_gross} {currency} - the document's stated total. Re-examine ONLY that
payable's structure: is a unit_price actually tax-inclusive? Is a tax on the wrong line/header?
Is a discount or charge missing or mis-typed? Do not just change gross_total to match - fix the
underlying components so the ERP recompute reaches the same number the document states, following
the same rules as before (nothing invented, correct placement). Common causes worth rechecking:
a withholding/retention tax left positive when it should be negative (it REDUCES what's owed); a
tax declared at both header and line level at once (double-counted); a header-level fee/charge
(service, agency, management, handling) that was left out of extra_charges entirely; a tax-inclusive
price used as unit_price without backing the tax back out. Return the FULL corrected JSON again,
same shape as before, for the whole file (all payables + declined).

--- your previous reply ---
{previous_json}
"""


MAX_RETRIES = 2  # bounded: up to 2 targeted retries (3 LLM calls total) per file


def process_file(pdf_path: Path, master: MasterData, usage: UsageTotals) -> dict:
    images = render_pdf_pages(pdf_path)
    if len(images) > config.MAX_PAGES_PER_CALL:
        print(f"  [warn] {pdf_path.name}: {len(images)} pages, capping at {config.MAX_PAGES_PER_CALL}")
        images = images[: config.MAX_PAGES_PER_CALL]

    user_text = USER_PROMPT_TEMPLATE.format(filename=pdf_path.name, page_count=len(images))
    raw = call_vision_json(SYSTEM_PROMPT, user_text, images, usage, pdf_path.name, max_tokens=8000)
    assembled_payables = [assemble_payable(p, master) for p in raw.get("payables") or []]

    # Bounded, targeted retries: each attempt gets the specific numeric gap fed back and can
    # fix a different root cause than the previous attempt (e.g. a tax-sign fix on attempt 1,
    # a still-remaining double-declared tax on attempt 2). Stops as soon as it foots, or after
    # MAX_RETRIES attempts - never an unbounded loop.
    for attempt in range(1, MAX_RETRIES + 1):
        mismatch = _first_mismatch(assembled_payables)
        if mismatch is None:
            break
        idx, booked, stated = mismatch
        payable = raw["payables"][idx]
        feedback = RETRY_FEEDBACK_TEMPLATE.format(
            idx=idx,
            invno=payable.get("invoice_number", ""),
            erp_gross=booked,
            stated_gross=stated,
            currency=payable.get("currency", ""),
            previous_json=json.dumps(raw, ensure_ascii=False),
        )
        retry_text = user_text + "\n\n" + feedback
        try:
            raw_retry = call_vision_json(
                SYSTEM_PROMPT, retry_text, images, usage,
                f"{pdf_path.name} (retry {attempt})", max_tokens=8000,
            )
            assembled_payables = [assemble_payable(p, master) for p in raw_retry.get("payables") or []]
            raw = raw_retry
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] retry {attempt} call failed, keeping previous attempt: {e}")
            break

    declined = []
    for d in raw.get("declined") or []:
        declined.append({"doc_type": str(d.get("doc_type", "")), "reason": str(d.get("reason", ""))})

    return {"file": pdf_path.name, "payables": assembled_payables, "declined": declined}


def _first_mismatch(payables: list[dict]):
    for i, p in enumerate(payables):
        ok, booked, stated = verify.check(p)
        if not ok:
            return (i, booked, stated)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_dir", default=str(config.DOCUMENTS_DIR))
    ap.add_argument("--out", dest="out_dir", default=str(config.OUTPUT_DIR))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", type=str, default=None, help="process a single file by stem")
    args = ap.parse_args()

    in_dir = Path(args.in_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(in_dir.glob("*.pdf"))
    if args.only:
        pdfs = [p for p in pdfs if p.stem == args.only]
    if args.limit:
        pdfs = pdfs[: args.limit]

    if not pdfs:
        print("No matching PDFs found.")
        return

    master = MasterData()
    usage = UsageTotals()
    t0 = time.time()
    n_payables = n_declined = n_mismatch_final = 0

    for i, pdf_path in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {pdf_path.name} ...", end=" ", flush=True)
        try:
            result = process_file(pdf_path, master, usage)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: {e}")
            result = {"file": pdf_path.name, "payables": [], "declined": [
                {"doc_type": "UNPROCESSED", "reason": f"pipeline error: {e}"}
            ]}

        bad = 0
        for p in result["payables"]:
            ok, booked, stated = verify.check(p)
            if not ok:
                bad += 1
        n_mismatch_final += bad
        n_payables += len(result["payables"])
        n_declined += len(result["declined"])

        out_path = out_dir / f"{pdf_path.stem}.json"
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)

        status = "OK" if bad == 0 else f"{bad} MISMATCH"
        print(f"{len(result['payables'])} payable(s), {len(result['declined'])} declined [{status}]")

    dt = time.time() - t0
    print("\n--- run summary ---")
    print(f"files: {len(pdfs)}  payables: {n_payables}  declined: {n_declined}  "
          f"mismatched-after-retry: {n_mismatch_final}")
    print(f"llm calls: {usage.calls}  tokens: {usage.input_tokens} in / {usage.output_tokens} out  "
          f"est. cost: ${usage.cost_usd:.4f}")
    print(f"elapsed: {dt:.1f}s")


if __name__ == "__main__":
    main()
