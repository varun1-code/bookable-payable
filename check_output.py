#!/usr/bin/env python3
"""Offline regression check: verify every output/*.json still books exactly
against the real erp.py, and catch a file that silently changed since it was
last reviewed - including one that changed but still happens to "mismatch",
which a plain pass/fail check would wave through as just another known
failure. This exists because a stray/uncommitted re-run once overwrote three
trusted output files with regressed content (DU-02, DU-03, DU-05) and the
only reason it was caught was a human noticing before committing.

    python check_output.py                  # check output/ against erp.py
    python check_output.py --out other_dir  # check a different directory

No API calls, no cost. Exits 0 only when every payable either foots exactly
or matches one of the KNOWN_EXCEPTIONS below *by the same gap* - update that
table by hand (with justification, in DESIGN.md) when a gap is genuinely
re-diagnosed or newly fixed. Exits 1 on anything else: a new mismatch, a
known exception whose gap moved, or one that quietly started footing (the
table is then stale and should be trimmed).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import config, verify

# The exact, understood gaps as of the last full review (see DESIGN.md,
# "Current state, and what's still imperfect"). Not a tolerance band - each
# entry pins the *specific* gap already investigated, so a file that starts
# failing by a DIFFERENT amount is flagged as CHANGED, not silently accepted
# as "still in the known list".
KNOWN_EXCEPTIONS = {
    "DU-06": (0.01, "per-line rounding vs. the document's own aggregate rounding"),
    "INV-13": (0.01, "per-line rounding across 25 lines vs. the document's own aggregate rounding"),
    "INV-06": (79.91, "genuine vision-model capability ceiling on a dense 24-line table"),
    "INV-26": (27.40, "document's own line items don't sum to its own printed subtotal"),
}
GAP_TOLERANCE = 0.005


def check_output(out_dir: Path) -> int:
    files = sorted(out_dir.glob("*.json"))
    if not files:
        print(f"No output files found in {out_dir}")
        return 1

    n_payables = n_declined = n_ok = n_known = 0
    n_regression = n_improved = n_changed = n_error = 0
    problems: list[str] = []

    for f in files:
        stem = f.stem
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            problems.append(f"{stem}: FAIL - output file is not valid JSON ({e})")
            n_error += 1
            continue

        declined = data.get("declined") or []
        payables = data.get("payables") or []
        if not declined and not payables:
            # Every document is either a payable or something to decline - there is no
            # third option. Empty/empty means the pipeline produced nothing for this
            # file, which is never correct (this is exactly the DU-02 regression shape:
            # a stray re-run silently wiped a reviewed, verified decline reason).
            problems.append(
                f"{stem}: REGRESSION - empty output (no payables, no declined entries). "
                f"Every document must be classified as one or the other."
            )
            n_regression += 1
            continue
        n_declined += len(declined)
        for i, p in enumerate(payables):
            n_payables += 1
            label = stem if len(payables) == 1 else f"{stem}[{i}]"
            try:
                ok, booked, stated = verify.check(p)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{label}: FAIL - erp.py raised {e!r}")
                n_error += 1
                continue
            gap = round(booked - stated, 2)

            if ok:
                if stem in KNOWN_EXCEPTIONS:
                    n_improved += 1
                    problems.append(
                        f"{label}: IMPROVED - now foots exactly (booked {booked}), but is "
                        f"still listed in KNOWN_EXCEPTIONS ({KNOWN_EXCEPTIONS[stem][1]}). "
                        f"Remove it from the table."
                    )
                else:
                    n_ok += 1
                continue

            if stem in KNOWN_EXCEPTIONS:
                expected_gap, reason = KNOWN_EXCEPTIONS[stem]
                if abs(abs(gap) - expected_gap) < GAP_TOLERANCE:
                    n_known += 1
                else:
                    n_changed += 1
                    problems.append(
                        f"{label}: CHANGED - known exception's gap moved from {expected_gap} to "
                        f"{abs(gap)} (erp booked {booked}, document states {stated}). This file "
                        f"may have been silently replaced by a stray re-run - verify by hand "
                        f"before trusting it, the same way the DU-02/DU-03/DU-05 incident was caught."
                    )
            else:
                n_regression += 1
                problems.append(
                    f"{label}: REGRESSION - erp booked {booked} but document states {stated} "
                    f"(gap {gap:+}), and this was not previously a known exception."
                )

    print("--- output regression check ---")
    print(f"files: {len(files)}  payables: {n_payables}  declined: {n_declined}")
    print(f"footing exactly: {n_ok}  known exceptions (unchanged): {n_known}")
    if n_improved or n_changed or n_regression or n_error:
        print(f"IMPROVED: {n_improved}  CHANGED: {n_changed}  REGRESSION: {n_regression}  ERROR: {n_error}")
    for line in problems:
        print(f"  {line}")

    if n_regression or n_changed or n_error:
        print("\nFAIL: one or more files regressed, changed unexpectedly, or errored.")
        return 1
    if n_improved:
        print("\nOK, but KNOWN_EXCEPTIONS in this script is now stale - update it (with a note in DESIGN.md).")
        return 0
    print("\nOK: matches the recorded, reviewed state exactly.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", dest="out_dir", default=str(config.OUTPUT_DIR))
    args = ap.parse_args()
    sys.exit(check_output(Path(args.out_dir)))


if __name__ == "__main__":
    main()
