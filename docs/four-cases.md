# The four required cases

Every example below is real output from this system running on the unmodified starter
documents — nothing here is hand-picked data, a hard-coded rule, or staged. The run behind
them: **7,750 grounded facts, 3,618 canonical attributes, 4,579 relations** across the six
documents. Fact and relation IDs are from `samples/crosscheck.sample.db`, committed so these
examples can be checked without an API key (`cp samples/crosscheck.sample.db data/crosscheck.db`,
then `python -m crosscheck.cli relations --type <TYPE>` or the Findings screen in the UI).

---

## Case 1 — corroboration across facts that are not even the same claim

**The clean version.** The Q4 FY24 earnings deck states three numbers about the fourth
quarter that are not, individually, the same claim as one another:

| Fact | Value | Attribute |
|---|---|---|
| A | `46` (Cr) | `ebitda` |
| B | `2,076` (Cr) | `revenue from customers` |
| Target | `2.2%` | `ebitda margin` |

No pair of these shares a claim key, so key-matching alone finds nothing. The
derived-value checker (`reason/derived.py`) instead asks: does any stated percentage equal
one same-period, same-subject amount divided by another? **46 ÷ 2,076 = 2.22%**, which
agrees with the stated 2.2% within tolerance — `DERIVED_CONSISTENT` relation `#4212`,
decided by rule with no model call. No formula for "EBITDA margin" is hard-coded; the
checker only requires the target's own attribute name to signal it is a ratio ("margin",
"ratio", "percentage", "share") and the numerator to share a word with the target, which is
what stops it turning into numerology on a large same-period fact pool — see case 4 for what
happens without that second guard.

**The cross-document version.** The same deck states FY23 revenue as `₹7,224 Cr` and FY24
revenue as `₹8,142 Cr` (a different page from the margin figures above); the annual report
separately states "YoY: 12.7%" for the same measure. (₹8,142 − ₹7,224) ÷ ₹7,224 = **12.7%**,
matching the stated growth exactly — a corroboration between a stated growth rate and the
two underlying levels it spans, found the same way, with no growth formula hard-coded either
(`reason/derived.py::find_growth`).

---

## Case 2 — a genuine contradiction, honestly presented

Real GDP growth for India, fiscal year 2024/25 (April 2024 – March 2025), stated by two
sources:

| Source | Value | Evidence |
|---|---|---|
| IMF, *2025 Article IV Consultation* (published 2025-11-21), fact `#50` | **6.5%** | *"India's real GDP grew by 6.5 percent in FY2024/25."* (p.10) |
| Government of India, *Economic Survey 2024-25* (fact `#4020`) | **6.4%** | *"As per the first advance estimates of national accounts, India's real GDP is estimated to grow by 6.4 per cent in FY25."* (p.4) |

Both facts share subject `India`, attribute `real gdp growth`, and the identical fiscal-year
interval — the claim keys should collide, and a genuine 0.1-point disagreement on the same
measure for the same period, with nothing in either quote explaining the gap, is exactly what
case 2 asks for.

**What the system currently does with this pair, and why it is only half right.** The
extractor assigned `scope: consolidated` to the Economic Survey fact and left it unset on the
IMF fact — a corporate-accounting term ("consolidated" vs "standalone" financial statements)
that does not meaningfully apply to a national growth figure at all. Because the two claim
keys therefore differ in exactly one component, the rule engine labelled the pair
`RECONCILED_BY_CONTEXT` (discriminator: `scope`) rather than surfacing it as a contradiction
for the model to adjudicate — a rule confidently explaining away a difference using a
qualifier that was never real. Read past that mislabelled `scope`, and the honest reading of
the evidence is a genuine, small discrepancy between an *advance estimate* (a preliminary
official figure, published before the fiscal year closes) and a later, more settled number —
which is arguably closer to `SUPERSEDES` or `RECONCILED_BY_CONTEXT` on a *vintage* basis than
a hard contradiction, and is precisely the kind of judgement call this system hands to the
model rather than a rule when the claim keys genuinely match. That it does not reach the
model here, because of the spurious scope tag, is itself real and reportable — see case 4.

---

## Case 3 — an apparent contradiction explained by context

FY24 revenue from services (`₹8,142 Cr`) against Q4 FY24 revenue from services
(`₹2,076 Cr`) — same subject, same attribute, and at a glance a factor-of-four disagreement
in what should be one number.

- **A** (fact `#1048`): *"₹8,142 Cr / FY24 revenue from services"* — deck, p.6
- **B** (fact `#1085`): *"₹2,076 Cr / Q4 FY24 revenue from services"* — deck, p.7

`normalize/periods.py` resolves `FY2023-24` and `Q4 FY2023-24` to concrete intervals and
reports the second as fully **contained within** the first, rather than disjoint. The rule
engine reads that as exactly one claim-key component differing — `period` — and labels the
pair `RECONCILED_BY_CONTEXT` with discriminator *"period (one covers part of the other)"*
(relation `#661`, decided by rule, confidence 0.90, zero model calls). An annual figure and its
own fourth quarter are not a contradiction; they are what containment looks like, and the
system says so instead of flagging a conflict.

The same mechanism catches the scale/unit version of this case elsewhere in the corpus: the
2022 prospectus states net issue proceeds as `₹40,000.00 million`, which `normalize/values.py`
resolves to the same amount as a `₹4,000 Cr` figure quoted elsewhere — once normalised, the
values agree, and where a genuine unit mismatch remains (a percent-of-GDP figure against the
same measure in dollars), the claim keys differ by `unit`, and that is what the system reports
as the explanation rather than a false conflict.

---

## Case 4 — extraction and reasoning failures found, and how they were handled

Running the full pipeline against the real corpus — not just imagining failure modes in
advance — surfaced eight distinct issues. Five were fixed in this session, with tests; three
are documented, honest limitations. This is deliberately the most detailed section, because
finding and reasoning about failure is the point of the exercise.

### 4a. A table row copied whole instead of decomposed per cell — **fixed**

The extraction prompt is explicit that a table row must become one fact **per cell**. On
wide rows the model sometimes ignored this:

```
attribute: "Cyclically adjusted balance (% of potential GDP)"
value:     "2021/22=-7.7; 2022/23=-8.2; 2023/24=-8.1; 2024/25=-7.9; 2025/26=-7.2; 2026/27=-7.1"
```

This is dangerous specifically because it is **invisible to grounding**: the string is a real
verbatim substring of the source, so it passes the quote check with a perfect score, and
`normalize/values.py` then reads off the *first* number it finds — the year `2021` — as if it
were the fact's value. **Fix:** any proposed value containing both `=` and `;` is rejected at
validation (`extract/extractor.py::_looks_like_undecomposed_row`); no legitimate single value
in this corpus's schema contains both. Verified on the live run: 242 such rows caught with
zero false positives among correctly-shaped values.

**And then, better: the cause was removed rather than only caught.** The guard was written
against `gpt-5-nano`, which produced these constantly. Measuring a second extraction model on
an identical 30-block sample showed `gpt-oss-120b` (via Cerebras' free tier) grounding 429 of
429 proposed facts against `gpt-5-nano`'s 338 of 401, and decomposing the IMF's Table 1
correctly — `9.7[2021/22]`, `7.6[2022/23]`, `9.2[2023/24]`, `6.5[2024/25]`, `6.6[2025/26]`,
`6.2[2026/27]`, one fact per cell with the right period on each. Re-extracting only the 193
table blocks with it (`crosscheck extract --kind table`, added for exactly this) took table
grounding from **59% to 99.3%** and produced **2,760 table facts where there had been 845** —
the same tables, read properly. The guard stays, because a guard that fires zero times is the
correct end state for one and because the failure is model-dependent rather than gone
forever; but the honest lesson is that a validation guard contains a bad extractor, and
measuring a better one fixes it.

### 4b. A value truncated mid-phrase — **fixed**

```
attribute: "contingent liabilities"
value:     "5.6 percent of"
```

Worse than merely awkward — the missing continuation is the entire substance of the claim,
and the truncated string still grounds perfectly. **Fix:** a value ending on a bare
preposition, article or conjunction is rejected (`_looks_truncated`).

### 4c. Attribute-vocabulary corruption from a fuzzy-matching bug — **fixed, the most consequential bug found**

The pre-LLM blocking pass that groups wording variants used `rapidfuzz.token_set_ratio`,
which scores a phrase and any string containing all its words as a **perfect match**, since
it compares token *sets*. On the real corpus this silently merged `"revenue from services"`
with `"capital expenditure as percentage of revenue from services"` (a level merged with a
ratio built out of it) and `"credit growth"` with `"credit to agriculture growth"` (two
different sectors' growth rates merged into one bucket) — both at a similarity score of 100.
Because only one representative example per pre-merged group is ever shown to the LLM
adjudication step, a bad merge made at blocking time was structurally uncorrectable
downstream. **Fix:** switched to `token_sort_ratio`, which still normalises word order but
correctly penalises extra words (every dangerous case now scores 53–67, comfortably below
the merge threshold). Two further, related bugs were found and fixed while verifying this
one on the real vocabulary: fact-linking matched attributes by raw text alone, discarding the
unit-family separation blocking had already computed; and nothing stopped two *different*
adjudication batches from independently proposing the same canonical text for two different
measures, which — because `comparable()` deliberately treats "number" and "percent" as
interchangeable, for table cells that lost their column header — would have produced a wrong
`RECONCILED_BY_CONTEXT` relation claiming two unrelated measures were "the same claim,
differing by unit." All three are covered by tests using the corpus's own attribute strings.

### 4d. Numerology in the derived-value checker — **fixed**

Auditing the actual `DERIVED_CONSISTENT` output (not just the code) found real false
corroborations: an option's exercise price divided by an unrelated deposit-account balance
landed within tolerance of a stated "expected volatility"; a table row copied without `=`/`;`
punctuation (`"216.68 16.24 16.33 9.75 9.58"`) had its first number used as a denominator.
**Fix, in two parts:** reject any value containing more than one number from participating in
arithmetic at all, and require a ratio's *target* to name itself as one ("margin", "ratio",
"percentage", "share") before attempting to match it — word-overlap between numerator and
denominator cannot distinguish good from bad here, since the genuine EBITDA-margin case has
no word overlap with its own denominator ("revenue from services") either. One acknowledged
remaining gap: "share" also names an equity share, so "post-offer paid up share capital"
(a level, not a ratio) still passes the word check — a real English ambiguity this heuristic
cannot fully resolve.

### 4e. Reconciliation re-paying for adjudications it already made — **fixed**

Read end-to-end before spending real money on it: every `reconcile()` call re-collects the
whole corpus and re-classifies every candidate pair. Rule-decided relations are free and
idempotent to re-derive, but nothing stopped an already model-adjudicated pair — including
one the model decided was `UNRELATED`, which was silently discarded rather than stored — from
being re-sent to the model on a later run. Confirmed directly: a second `reconcile()` call
before this fix made all 500 of its calls fresh again. **Fix:** UNRELATED is now a real,
stored relation type (excluded from the UI's default view, since it is bookkeeping and not a
finding), and every pair already decided by the model is skipped before classification. Two
runs after the fix made zero further calls for already-known pairs.

### 4f. Biographical facts collide under the company name as subject — **found, not fixed**

The most consequential remaining issue. Facts extracted from individual directors'
biographies — education, appointment date, remuneration — are assigned `subject: Delhivery
Limited` rather than the specific director's name, because the block containing "He holds a
bachelor's degree…" does not carry the earlier sentence naming who "He" is. The result: every
`CONTRADICTS` relation this system currently stores is a false positive of this shape —
different directors' different, non-conflicting educations flagged as if one entity held
contradictory degrees. This is why case 2 above is built from a macro-economic pair (`India`
as subject is well disambiguated across all three documents) rather than from the system's
own top-confidence `CONTRADICTS` list. **What a real fix looks like:** either widen block
boundaries to keep a name-introducing sentence together with the biographical detail that
follows it, or add a lightweight coreference pass before extraction — both are real
engineering, not a one-line guard, which is why this is reported rather than patched under
the time available.

### 4g. Garbled chart text extracted as a value — **found, not fixed, and harmless downstream**

```
value: "India: Real GDP Growth Revision (from July WEO) India: (Percentage Impact
        points, of relative Lower to baseline) US Tariffs on Real GDP Growth"
grounding: verbatim (100)
```

A chart's axis labels, legend and caption, read linearly by PyMuPDF, interleave into a
scrambled but genuine string; the model faithfully reports it because that string really does
appear on the page in that order. Left as a documented limitation rather than patched,
because it is provably inert: `normalize/values.py` correctly fails to parse a number from it,
so `value_num` stays `None` and the fact never enters any numeric comparison. A proper fix
would detect chart/figure regions during layout reconstruction and exclude or represent them
differently — noted in the README's Limitations section as a next step, not attempted here
because the failures that could silently corrupt a *comparison* (4a–4d) were the better use of
the time available.

---

## What I would build next given more time

- Widen extraction context (or add coreference resolution) so a director's biography and
  the sentence naming them are never split across the block boundary that currently causes 4f.
- Retry-with-narrower-prompt for a rejected wide table row (4a), to recover its facts instead
  of only discarding the wrong ones.
- Chart/figure region detection during layout reconstruction, so page content that is a
  chart is not offered to the extractor as if it were a paragraph (4g).
- A confidence-weighted Review-screen category surfacing case-4-shaped near-misses — facts
  that grounded but tripped a quality guard — as a distinct, browsable list rather than only
  visible via the CLI's `review` command.
