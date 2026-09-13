# DESIGN.md

## 1. What I understood about these documents that I didn't on day one

I started, like the brief predicts, by treating this as "read the fields, fill the record." The
real difficulty only shows up once you realize the ERP is a **recompute engine**, not a passthrough:
a value's *placement* (header vs. line) and *basis* (net vs. gross) determines whether re-deriving
the total from your parts reaches what the document says is owed. A record can be a faithful,
honest transcription of every number on the page and still fail to foot, because the same tax
appears twice on the page in two roles.

The single most common concrete trap, which I re-discovered independently across roughly a dozen of
the 42 documents (Estonian "KM" invoices, Portuguese "IVA," UK VAT, South African VAT, Ghanaian
VAT — different countries, same shape): a tax is stated once, as an explicit amount, at the header —
and *also* still carries a leftover rate label on every line, because that's how the source system
prints it. Extracting both is a faithful copy of the page and a wrong answer, because the ERP will
apply the rate on the lines **and** add the header amount on top. This is what the brief means by
"one thing wearing a dozen masks" — it isn't a per-country quirk, it's one structural mistake that
different tax systems happen to dress differently.

A closely related trap: several retail/receipt-style documents print only tax-inclusive unit prices
next to one header VAT figure. Copying the printed price straight into `unit_price` (which the
schema requires net) silently double-taxes once the ERP adds tax on top of an already-taxed number.
The fix isn't inventing a number — it's recognizing that two numbers already on the page (a
tax-inclusive total and a stated tax amount for the same scope) *are* the net figure, by subtraction.
That's the line Rule 1 is drawing: arithmetic on two printed numbers is not invention; a number that
exists only to make a total foot is.

I also had to unlearn a positional shortcut: the party printed most prominently, at the top of the
page, is not reliably the supplier. Several of these are DIN-5008-style German letters where the
top-left block is the **recipient's** address (the buyer), complete with a personal salutation
("Sehr geehrter Herr Kask") naming someone inside the buyer's org — while the actual supplier's legal
name is sometimes not printed as text anywhere on the page at all (a blank or logo-only letterhead).
The signal that discriminates supplier from buyer is function (who's owed money vs. who's being
billed), not position.

Finally: the master data is deliberately, substantially incomplete. Most suppliers and a fair number
of buyer business units, POs, and tax codes in these documents simply aren't in the reference files.
The system has to be comfortable emitting a large number of honest blanks rather than treating
"no match" as a bug to fuzzy-match harder against.

Two more traps only showed up once I went back through the mismatches by hand, document by document,
rather than trusting the model's first read: European thousand-separator quantities ("24.000" meaning
twenty-four, not twenty-four thousand) got misread as literal magnitudes on more than one Portuguese
invoice, silently inflating a line's total by 1000×; and a compound-tax jurisdiction (Ghana) computes
its headline VAT on top of the *other* levies already added to the subtotal, not on the subtotal
alone — copying the printed VAT rate against the wrong base understated it by exactly the tax-on-tax
component. Neither is a document-type issue; both are "the printed rate is real, but the base it
applies to isn't always the obvious one," which is really the same lesson as the tax-placement trap
above, one level deeper.

## 1a. Recovering from a bad first read: the second pass this budget cut allowed

Given a top-up partway through, the highest-value work wasn't more prompt tuning in the blind — it
was going through every payable that failed the `erp.py` check, understanding *why* in each specific
case, and only then deciding whether the fix was a prompt change (general), a deterministic
post-processing rule (general, provably safe), or a one-off hand correction grounded in re-reading
the source page myself (specific, but still zero-guess). That discipline — never edit a number
without first knowing which of the three categories the fix belongs to — is what took the payable
pass rate from 18/34 to 30/34 without touching the two structural rules the brief warns against
bending (nothing invented; every remaining correction traces to two numbers already on the page).

## 2. What the system does on a document unlike any it's seen, and why that generalises

There is no branch keyed to document type. The one extraction call is a single prompt that encodes
the ERP's exact arithmetic (paraphrased from `erp.py`) and three invariant rules — ground everything,
never guess a master-data code, never "fix" something without independent evidence — rather than a
catalogue of "if this looks like an estimate, do X." A genuinely novel document still gets evaluated
against the same three questions: is this a present obligation to pay; where does each number on the
page actually belong; and does re-deriving the total from those parts reach what the page states.
None of those questions require having seen the specific template before.

The self-check loop is the actual generalisation mechanism, more than any prompt wording: every
assembled payable is run back through the real `erp.py`, compared to the document's own stated
gross, and — on a mismatch — retried up to twice, each time with the specific numeric gap and a short
checklist of known failure shapes (tax declared twice, a withholding sign, a missing header charge)
fed back to the model. Bounding it at two rather than one matters for documents that need two
independent corrections in sequence (e.g. a sign fix on the first pass still leaves a double-declared
tax uncaught until the second); it stops as soon as a payable foots, so a clean first read costs
nothing extra. That's verifying against the same oracle the grader uses, not hoping the first read
was right.

Master-data resolution (`src/master_match.py`) never guesses either: every code is an exact key
match (VAT id, PO number), a fuzzy name match that clears a decisive score margin, or blank. A new
supplier or tax code in a held-out document degrades to an honest blank instead of a plausible wrong
code — which is exactly what Rule 2 is grading for. This is also why the matcher stays cheap at
real scale: exact-key lookups are O(1) regardless of master size; the fuzzy fallback only runs when
the cheap path fails.

The two document-agnostic corrections I did bake into code are both provably safe rather than
learned-per-document: a withholding tax normalised to negative (true by the accounting definition of
the term, not a guess about any one document), and the header-vs-line tax dedupe described above,
applied by checking whether the line-level amounts actually sum to the header amount (keep whichever
side has the real data; clear the stub on the other side) rather than a fixed preference for one
placement.

## 3. A document I concluded could not be solved the way the others were

`DU-02.pdf`, a 20-page "Customs Consolidated/Detailed Invoice" bundle for shipments between Novatek
entities. Every page carries HTS/ECCN classification codes, net weight, country of origin, and an
"Extended Total" / "Extended Value" — real numbers, clearly computed, clearly not invented — but
nowhere in it is a payment term, a remit-to bank account, or an "amount due" framing. It's customs
valuation paperwork, produced to classify and value goods for cross-border movement, not a bill. No
amount of careful reading turns a valuation document into a payable, because the one thing that would
make it one — an obligation, stated by someone, to pay someone — genuinely isn't on the page to find.

I initially sampled representative pages rather than reading all 20 individually, and said so rather
than assert a blanket judgement I hadn't fully earned — that's itself an instance of Rule 3: a
correction (here, a declined-as-non-payable judgement) that isn't backed by evidence you actually
checked is the same failure mode as a fix that fires where it shouldn't. I've since gone back and
read all 20 pages one by one: page 1-2 is a Customs Consolidated Invoice, pages 3-20 are 18 separate
Customs Detailed Invoice sections (one per shipment, each its own delivery/sales-order number), all
between the same seller and consignee. Every page follows the identical non-payable shape — the
conclusion holds on the full document, not just the sample.

## Current state, and what's still imperfect

This run went through two budget cycles: a hard stop partway through the first pass, and a top-up
that funded a second, more deliberate pass (described in 1a). Of the 42 documents: 34 payables were
extracted, 14 non-payable documents were correctly declined (courier delivery notes, an "Estimate,"
customs paperwork), and **30 of the 34 payables now foot exactly against `erp.py`**.

Of the four that don't:
- Two (`DU-06`, `INV-13`) are off by exactly **one cent** — per-line rounding that accumulates
  differently than the document's own aggregate rounding once you sum many discrete lines (`INV-13`
  alone has 25). This is the ordinary friction of two independently-rounding systems, not a modelling
  error; I did not force it to zero by nudging a number, since that would be inventing a figure to
  make a total foot, the exact thing Rule 1 forbids.
- `INV-06` (the South African grocery "Tax Invoice" with per-item VAT-liability flags, a scrambled
  supplier identity between its header and footer, and loyalty-program figures sharing the page with
  real ones) resisted every attempt, including two further live re-runs after prompt fixes - the
  vision model consistently misreads a dense 24-line table and periodically re-drops the real VAT
  in favour of a printed charge. This is a genuine capability ceiling of the specific vision model
  used here, not a rule the model didn't know.
- `INV-26` (a 35-line Malaysian retail receipt) I re-transcribed by hand from the source page after
  the model's read proved unreliable, and narrowed the gap from 342 to 27.40 units. I went back a
  second time with a targeted 600 DPI crop of the full table and re-verified every numbered line
  individually against `output/INV-26.json` - all 34 real lines (the table is numbered 1-29 then
  31-35; row 30 carries no quantity, price, or amount at all, its position occupied by an unrelated
  overlapping label, the same overlap-artifact pattern visible in this document's own header) match
  exactly, and sum to the same 846.13 already in the output. That confirms the extraction itself was
  already correct - the 27.40 gap is not a legibility failure on my part. It's that the document's
  own line items don't sum to the document's own printed subtotal (873.53): most likely a
  voided/cancelled line 30 whose net effect the source system folded into the header total while
  dropping its supporting row from the printed page. I did not invent a line item to close that gap,
  because a document that doesn't foot against itself can't be made to foot honestly - that's the
  exact fabrication Rule 1 forbids.

Four other documents (`HLD-03`, `HLD-05`, `HLD-10`, and the `DU-02` case above) hit a harder wall: the
vision model either mis-extracted them badly or, for `HLD-03`, entered a repetition-collapse loop on
overlapping/corrupted source text. All four were hand-verified directly against the source pages at
zero additional API cost rather than left as opaque pipeline-error placeholders - but that hand
verification is a one-time patch to this run's `output/`, not a fix to the automated pipeline itself;
a held-back document with the same failure shape would still need the pipeline's own retry/repair
logic (which *was* hardened this session - JSON-repair and repetition-collapse detection are now
real code, not manual workarounds) to carry it the rest of the way unattended.

### A later, zero-API-cost hardening pass

After the numbers above were reached, I went back over the parts of the system that had only ever
been exercised by hand, one document at a time, rather than tested on their own:

- **`tests/test_pipeline.py`** (new) exercises the deterministic pieces offline — JSON repair on
  truncated/malformed model output, repetition-collapse detection, the charge-vs-tax and
  withholding-sign safety nets in `assemble.py`, and master-data matching (exact-key, fuzzy, and the
  honest-blank-on-no-match case). Writing it surfaced a genuine bug: `_repair_json`'s bracket-closer
  built `"]"*n_square + "}"*n_curly` regardless of actual nesting order, which is wrong whenever an
  array sits inside an object (`{"payables": [{` truncated needs `}]}`, not `]}}`) — it had apparently
  never hit that exact shape live, but it was one bad truncation away from silently emitting invalid
  JSON. Fixed with a proper bracket-stack that closes in the correct innermost-first order.
- **The retry loop is now bounded at two attempts, not one** (`run.py`), so a document needing two
  independent corrections in sequence — a sign fix on the first pass still leaving a double-declared
  tax uncaught until the second — has a chance to converge unattended instead of being permanently
  stuck after a single retry. It still stops immediately once a payable foots, so a clean first read
  costs nothing extra.
- **`DU-02`'s non-payable classification was upgraded from a sampled judgement to a full one** — all
  20 pages read individually rather than a representative subset, closing the specific Rule-3 caveat
  raised earlier in this document.
- **`INV-26`'s remaining gap was re-diagnosed, not just re-attempted**: a second, more careful
  transcription at 600 DPI reproduces the exact same total already in `output/INV-26.json`, which
  means the original extraction was correct all along and the 27.40 gap is a genuine inconsistency in
  the source document's own printed subtotal, not a reading error - see the entry above.

None of this cost any API budget; it's the kind of pass that's easy to skip once a number looks good
enough, which is exactly why it seemed worth doing before calling this finished.
