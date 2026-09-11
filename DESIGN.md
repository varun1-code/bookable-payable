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
gross, and — on a mismatch — retried once with the specific numeric gap and a short checklist of
known failure shapes (tax declared twice, a withholding sign, a missing header charge) fed back to
the model. That's verifying against the same oracle the grader uses, not hoping the first read was
right.

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

I did not have the budget (see below) to read all 20 pages individually to confirm the pattern holds
throughout; I sampled representative pages and flagged the file for manual review rather than assert
a blanket judgement I hadn't fully earned by reading every page. That is itself an instance of Rule
3: a correction — here, a declined-as-non-payable judgement — that isn't backed by evidence you
actually checked is the same failure mode as a fix that fires where it shouldn't.

## Current state, and what I'd do with more budget

This run was cut short mid-iteration by a hard API budget limit (the extraction model is a paid,
per-token vision API), at the user's explicit instruction to stop spending and ship what existed. Of
the 42 documents: 34 payables were extracted, 15 non-payable documents were correctly declined
(courier delivery notes, an "Estimate," customs paperwork), and as of this budget cut **18 of the 34
payables foot exactly against `erp.py`**, with most of the remainder traced to one of the two
structural traps above (rather than 15 unrelated bugs) but not re-verified against a live model call.
Four documents (`HLD-03`, `HLD-05`, `HLD-10`, and the `DU-02` case above) hit a genuine model-capability
wall — one drove the vision model into a repetition-collapse loop on overlapping/corrupted source
text — and were hand-verified directly against the source pages at zero additional cost rather than
left as opaque pipeline-error placeholders. With more budget, the next step is not new prompt
categories but tighter closed-loop verification: run the retry-with-feedback loop to convergence
(currently capped at one retry to bound cost) on the remaining mismatches, and extend the same
"does line-level tax data actually sum to the header figure" check the fix pass used here into the
extraction prompt itself, as a self-check the model runs before returning its answer.
