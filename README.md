# The Bookable Payable — submission

Turns a folder of supplier PDFs into ERP-ready autodraft JSON. The original assignment brief is
preserved at [`ASSIGNMENT.md`](ASSIGNMENT.md); the design writeup answering its three questions is
at [`DESIGN.md`](DESIGN.md).

## How it works, in one paragraph

For each PDF, every page is rendered to an image and sent in a single call to a vision-capable LLM
(swappable via env vars — currently wired to Hive's `hive/vision-language-model`) with a prompt that
embeds the ERP's exact recompute contract (from `erp.py`) and the brief's three rules verbatim. The
model classifies what's in the document and extracts raw components — it never resolves master-data
codes itself. A small deterministic layer (`src/master_match.py`) then matches supplier/buyer/tax/
payment-term/PO codes against `master_data/*.json` by exact key first (VAT id, PO number) and falls
back to fuzzy name matching only where no stronger key exists, so it stays cheap even at the "hundreds
of thousands of rows" scale the brief describes. Every assembled payable is recomputed through the
real `erp.py` before being written out; a mismatch triggers up to two bounded retries with the specific gap
fed back to the model. A retry can only ever improve a file's payables, never regress one that already
footed (`run.py::_reconcile_retry`) — see `DESIGN.md` for why this shape, not a bank of per-document rules.

## Setup

```
pip install -r requirements.txt
cp .env.example .env        # fill in your own API key
```

`.env` needs:

```
HIVE_API_KEY=...
HIVE_BASE_URL=https://api.thehive.ai/api/v3/     # any OpenAI-compatible vision endpoint works
HIVE_VISION_MODEL=hive/vision-language-model
```

Swap the base URL/model for OpenAI, or any other OpenAI-compatible vision API — nothing else in the
code is provider-specific (see `src/hive_client.py`).

## Run

```
python run.py
```

Reads every `*.pdf` in `documents/`, writes one `output/<name>.json` per input, and prints a summary
(payables/declines found, how many still fail the ERP recompute, token spend). Useful flags:

```
python run.py --limit 3          # smoke-test on the first 3 files
python run.py --only INV-01      # process a single file by stem
python run.py --in other_dir --out other_output
```

## Tests

```
python -m pytest tests/ -v
```

Offline regression tests (no API calls, no cost) for the deterministic parts of the pipeline: JSON
repair on truncated/malformed model output, rejection of genuinely malformed (not just truncated)
bracket structure, repetition-collapse detection, the charge-vs-tax and withholding-sign safety nets
in `assemble.py` (now Decimal-based internally, so summing several reclassified charges can't drift a
fraction of a cent the way float addition can), master-data matching (exact-key, fuzzy, and the
honest-blank-on-no-match case), and — via mocked `call_vision_json`/`render_pdf_pages` — that a retry
can never drop or regress a payable that already footed. These exist because that logic was
previously only ever exercised by hand, one document at a time, during the live run; several of these
tests caught real bugs (see `DESIGN.md`) rather than just documenting existing behaviour.

## Checking trusted output hasn't drifted

```
python check_output.py
```

Re-verifies every `output/*.json` payable against the real `erp.py` and reports anything that isn't
either footing exactly or matching one of a short, explicit table of already-understood exceptions
(by the *same* gap, not just "still mismatched"). No API calls. This exists because a stray/uncommitted
re-run silently overwrote several trusted output files with regressed content more than once during
development (see `DESIGN.md`) — this is the guard against that happening unnoticed again, including in
CI or as a pre-commit check.

## Repo layout

```
run.py                 the one command (also: retry-vs-regression reconciliation)
check_output.py        offline regression check: output/*.json vs erp.py, no API calls
src/
  render.py             PDF -> page images
  prompt.py             the extraction prompt (the actual "brain" of the system)
  hive_client.py         vision-API wrapper: retries, repetition-collapse detection, JSON repair
  assemble.py             raw extraction -> final AUTODRAFT_SCHEMA record (Decimal internally)
  master_match.py          deterministic master-data matching (indexed, not a per-lookup scan)
  verify.py                 calls the real erp.py to check every payable foots
tests/test_pipeline.py  offline tests for all of the above (no API calls)
documents/, master_data/, erp.py, AUTODRAFT_SCHEMA.md, sample_autodraft.json   (as given)
output/*.json            generated results for the open document set
ASSIGNMENT.md             the original brief
DESIGN.md                  design writeup
```

## A note on `_needs_review`

If a file's `output/<name>.json` ever gains a top-level `"_needs_review": "json_repaired"` key
(outside `AUTODRAFT_SCHEMA.md`'s three defined keys), it means the model's raw reply needed JSON
repair to parse — i.e. it wasn't clean, directly-parseable output, so a line item or trailing field
could in principle be missing even though the record parses. None of the 42 files in this submission
carry this flag. It's there so a held-back run can tell repaired-but-plausible output apart from a
clean extraction instead of treating them as indistinguishable.

## Known gaps in the current output

Of 42 documents: 34 payables extracted, 14 correctly declined as non-payables, **30 of the 34
payables foot exactly** against `erp.py`. The remaining 4 (two off by a single cent of rounding, two
with a real residual gap) are named and explained — including exactly what I did and didn't verify
by hand versus through the automated pipeline — in the "Current state" section at the end of
`DESIGN.md`, rather than presenting the run as cleaner than it is.
