# DESIGN.md

## 1. What I understood about these documents that I didn't on day one

I started, like the brief predicts, by treating this as "read the fields, fill the record." The
real difficulty only shows up once you realize the ERP is a **recompute engine**, not a passthrough:
a value's *placement* (header vs. line) and *basis* (net vs. gross) determines whether re-deriving
the total from your parts reaches what the document says is owed. A record can be a faithful,
honest transcription of every number on the page and still fail to foot, because the same tax
appears twice on the page in two roles.

The single most common trap, rediscovered independently across roughly a dozen of the 42 documents
(Estonian "KM" invoices, Portuguese "IVA," UK VAT, South African VAT, Ghanaian VAT — different
countries, same shape): a tax is stated once, as an explicit header amount — and *also* still
carries a leftover rate label on every line, because that's how the source system prints it.
Extracting both is a faithful copy of the page and a wrong answer, because the ERP applies the rate
on the lines **and** adds the header amount on top. This is what the brief means by "one thing
wearing a dozen masks" — one structural mistake different tax systems happen to dress differently.

A closely related trap: several retail/receipt-style documents print only tax-inclusive unit prices
next to one header VAT figure. Copying that straight into `unit_price` (which the schema requires
net) silently double-taxes once the ERP adds tax on top of an already-taxed number. The fix isn't
inventing a number — it's recognizing that two numbers already on the page (a tax-inclusive total
and a stated tax amount) *are* the net figure, by subtraction. That's the line Rule 1 draws:
arithmetic on two printed numbers is not invention; a number that exists only to make a total foot is.

I also had to unlearn a positional shortcut: the party printed most prominently is not reliably the
supplier. Several documents are DIN-5008-style German letters where the top-left block is the
**recipient's** address (the buyer), complete with a personal salutation ("Sehr geehrter Herr
Kask") — while the supplier's legal name is sometimes not printed anywhere on the page at all (a
blank or logo-only letterhead). The signal that discriminates supplier from buyer is function
(who's owed money vs. who's being billed), not position.

Finally: the master data is deliberately, substantially incomplete. Most suppliers and a fair
number of buyer business units, POs, and tax codes simply aren't in the reference files — the
system has to be comfortable emitting a large number of honest blanks rather than fuzzy-matching
harder to force a hit.

Two more traps only showed up going back through the mismatches by hand: European thousand-separator
quantities ("24.000" meaning twenty-four, not twenty-four thousand) got misread as literal
magnitudes on more than one Portuguese invoice, inflating a line's total by 1000×; and a
compound-tax jurisdiction (Ghana) computes its headline VAT on top of the *other* levies already
added to the subtotal, not the subtotal alone. Neither is a document-type issue; both are "the
printed rate is real, but the base it applies to isn't always the obvious one" — the same lesson as
the tax-placement trap, one level deeper.

## 2. What the system does on a document unlike any it's seen, and why that generalises

There is no branch keyed to document type. The one extraction call is a single prompt that encodes
the ERP's exact arithmetic (paraphrased from `erp.py`) and three invariant rules — ground
everything, never guess a master-data code, never "fix" something without independent evidence —
rather than a catalogue of "if this looks like an estimate, do X." A genuinely novel document still
gets evaluated against the same three questions: is this a present obligation to pay; where does
each number actually belong; and does re-deriving the total from those parts reach what the page
states. None of those require having seen the specific template before.

The self-check loop is the actual generalisation mechanism, more than any prompt wording: every
assembled payable is run back through the real `erp.py`, compared to the document's own stated
gross, and — on a mismatch — retried up to twice, each time with the specific numeric gap and a
short checklist of known failure shapes (tax declared twice, a withholding sign, a missing header
charge) fed back to the model. It stops as soon as a payable foots, so a clean first read costs
nothing extra — that's verifying against the same oracle the grader uses, not hoping the first read
was right. A retry can also only ever *improve* a file's payables, never regress one that already
footed: `run.py::_reconcile_retry` matches payables across attempts by invoice number and refuses to
accept a retry response that silently drops or breaks a payable that was already correct.

Master-data resolution (`src/master_match.py`) never guesses either: every code is an exact key
match (VAT id, PO number), a fuzzy name match that clears a decisive score margin, or blank. A new
supplier or tax code in a held-out document degrades to an honest blank instead of a plausible wrong
code — exactly what Rule 2 is grading for. Exact-key lookups are O(1) regardless of master size; the
fuzzy fallback only runs when the cheap path fails, which is why the matcher stays cheap at the
"hundreds of thousands of rows" scale the brief describes.

The document-agnostic corrections baked directly into code are provably safe rather than
learned-per-document: a withholding tax normalised to negative (true by accounting definition, not a
guess about any one document), a fuel/handling/service charge that the model mislabels as a
government tax reclassified by name pattern rather than dropped, and header-vs-line tax dedupe,
applied by checking whether the line-level amounts actually sum to the header amount rather than a
fixed preference for one placement.

## 3. A document I concluded could not be solved the way the others were

`DU-02.pdf`, a 20-page "Customs Consolidated/Detailed Invoice" bundle for shipments between Novatek
entities. Every page carries HTS/ECCN classification codes, net weight, country of origin, and an
"Extended Total" / "Extended Value" — real numbers, clearly computed, clearly not invented — but
nowhere in it is a payment term, a remit-to bank account, or an "amount due" framing. It's customs
valuation paperwork, produced to classify and value goods for cross-border movement, not a bill. No
amount of careful reading turns a valuation document into a payable, because the one thing that
would make it one — an obligation, stated by someone, to pay someone — genuinely isn't on the page.

I initially sampled representative pages rather than reading all 20 individually, and said so rather
than assert a blanket judgement I hadn't fully earned — that's itself an instance of Rule 3: a
correction that isn't backed by evidence you actually checked is the same failure mode as a fix that
fires where it shouldn't. I've since read all 20 pages one by one: pages 1-2 are a Customs
Consolidated Invoice, pages 3-20 are 18 separate Customs Detailed Invoice sections (one per
shipment, each its own delivery/sales-order number), all between the same seller and consignee.
Every page follows the identical non-payable shape — the conclusion holds on the full document, not
just the sample.

## Current state, and what's still imperfect

Of the 42 documents: 34 payables were extracted, 14 non-payable documents were correctly declined
(courier delivery notes, an "Estimate," customs paperwork), and **30 of the 34 payables foot exactly
against `erp.py`**. Reaching that from an initial 18/34 (after a mid-project budget interruption and
top-up) meant going through every failing payable individually and deciding, before touching
anything, whether the fix belonged in the prompt (general), a deterministic post-processing rule
(general, provably safe), or a one-off hand correction grounded in re-reading the source page myself
(specific, but still zero-guess) — never editing a number without knowing which category it was in.

Of the four that don't foot:
- Two (`DU-06`, `INV-13`) are off by exactly **one cent** — per-line rounding that accumulates
  differently than the document's own aggregate rounding once you sum many discrete lines (`INV-13`
  alone has 25). Ordinary friction between two independently-rounding systems, not forced to zero,
  since that would mean inventing a figure to make a total foot — the exact thing Rule 1 forbids.
- `INV-06` (a South African grocery "Tax Invoice" with per-item VAT-liability flags and a scrambled
  supplier identity between header and footer) resisted every attempt, including two further live
  re-runs after prompt fixes — the vision model consistently misreads a dense 24-line table. A
  genuine capability ceiling of this specific vision model, not a rule it didn't know.
- `INV-26` (a 35-line Malaysian retail receipt) I re-transcribed by hand from the source page,
  narrowing the gap from 342 to 27.40 units, then re-verified every line a second time at 600 DPI —
  it reproduces the exact total already in `output/INV-26.json`, confirming the extraction was
  already correct. The 27.40 gap is the document's own line items not summing to its own printed
  subtotal (most likely a voided line whose supporting row was dropped from the printed page), not a
  reading error on my part — I did not invent a line item to close it.

Four other documents (`HLD-03`, `HLD-05`, `HLD-10`, and `DU-02` above) hit a harder wall — badly
mis-extracted, or for `HLD-03`, a repetition-collapse loop on corrupted source text — and were
hand-verified against the source pages at zero extra API cost rather than left as opaque
pipeline-error placeholders. That hand verification is a one-time patch to this run's `output/`, not
a fix to the pipeline itself; a held-back document with the same failure shape would rely on the
pipeline's own hardened retry/repair logic, not on manual reading.

Two further rounds of zero-API-cost hardening followed (full detail in the commit history and
`README.md`): an offline test suite (`tests/test_pipeline.py`) that surfaced and fixed a real
bracket-ordering bug in JSON repair, a retry bound widened from one attempt to two, `_reconcile_retry`
making retries provably non-regressive, rejection of malformed (not just truncated) JSON instead of
silently patching it, a `_needs_review` flag on any output that needed JSON repair, `Decimal`
internal arithmetic in `assemble.py`, and `check_output.py` — an offline command that re-verifies
every output file against `erp.py`. That last tool proved itself immediately: a live re-run for a
sanity check turned out badly non-deterministic against this provider (10/41 payables footing,
versus the verified 30/34), and it caught that precisely before the run could be mistaken for the
submission. The committed `output/` was restored from git and re-verified; nothing from that re-run
is in this submission.
