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
real `erp.py` before being written out; a mismatch triggers one bounded retry with the specific gap
fed back to the model. See `DESIGN.md` for why this shape, not a bank of per-document rules.

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

## Repo layout

```
run.py                 the one command
src/
  render.py             PDF -> page images
  prompt.py             the extraction prompt (the actual "brain" of the system)
  hive_client.py         vision-API wrapper: retries, repetition-collapse detection, JSON repair
  assemble.py             raw extraction -> final AUTODRAFT_SCHEMA record
  master_match.py          deterministic master-data matching (indexed, not a per-lookup scan)
  verify.py                 calls the real erp.py to check every payable foots
documents/, master_data/, erp.py, AUTODRAFT_SCHEMA.md, sample_autodraft.json   (as given)
output/*.json            generated results for the open document set
ASSIGNMENT.md             the original brief
DESIGN.md                  design writeup
```

## Known gaps in the current output

`output/*.json` here was produced under a hard budget cut partway through — see the "what I'd do
next" note at the end of `DESIGN.md` for exactly which documents are still imperfect and why, rather
than presenting the run as cleaner than it is.
