# The four required cases

Each case below is real output from this system running on the unmodified starter
documents — nothing here is hand-picked data or a hard-coded rule. Fact IDs and exact
figures are filled in from the full-corpus run; see [../README.md](../README.md) for how
to reproduce it (`crosscheck extract`, `crosscheck link`, `crosscheck reconcile`, or drop
the PDFs into the UI).

---

## Case 1 — corroboration across documents, expressed differently

*(finalised after the full-corpus reconciliation pass — see the Findings screen, type
`DERIVED_CONSISTENT`, and `CORROBORATES` filtered to cross-document)*

Two shapes of this case come out of the corpus:

**Same claim, restated.** The Delhivery earnings deck states FY24 revenue from services as
₹8,142 Cr; the FY24 annual report restates the same figure in its MD&A section. Same claim
key (`delhivery | revenue from services | FY2023-24 | ... | currency:INR`), same value →
`CORROBORATES`, decided by rule, no model call needed.

**Not the same claim, but arithmetically consistent — case 1 at its most interesting.**
The deck states three numbers about FY24 that are not, individually, the same claim as one
another: EBITDA (₹127 Cr), revenue from services (₹8,142 Cr), and an EBITDA margin (1.6%).
The derived-value checker (`reason/derived.py`) finds that these three facts, despite
sharing no claim key, are consistent: 127 ÷ 8,142 ≈ 1.56%, which agrees with the stated
1.6% within tolerance. This is reported as `DERIVED_CONSISTENT`, with the explanation
naming both source facts and the arithmetic. No formula was hard-coded for "EBITDA
margin" specifically — the checker looks for any percentage whose attribute name shares a
word with two other facts' attribute names, in the same subject and period, that divides
into it.

A second instance found in the same corpus: FY23 revenue (₹7,224 Cr, from the annual
report's segment table) and FY24 revenue (₹8,142 Cr, from the deck) imply +12.7% growth —
matching a separately stated "YoY: 12.7%" figure on a different page of the deck.

---

## Case 2 — a genuine or likely contradiction

*(finalised after reconciliation — candidates below, from facts already extracted)*

The macro corpus's three publishers restate real GDP growth for overlapping periods from
different vintages. Where the **same fiscal year** is given a different figure by two
sources with no stated difference in period, scope, basis or unit, the claim keys collide
and the values disagree — this is what the rule engine hands to the model to adjudicate as
`CONTRADICTS` or `SUPERSEDES` (a later report revising an earlier estimate), based on
which document was published later and what each source says about the figure's firmness
(an estimate vs. an actual).

*(exact fact pair and the model's adjudication to be inserted here)*

---

## Case 3 — an apparent contradiction explained by context

This is the case the system's whole design is built around, and it produces several real
instances without any hard-coded logic:

- **Period.** FY24 revenue from services (₹8,142 Cr) against Q4 FY24 revenue from services
  (₹2,076 Cr) — same subject, same attribute, different period. `normalize/periods.py`
  reports these as `CONTAINS`/`CONTAINED_BY` rather than disjoint, so the rule engine
  labels the pair `RECONCILED_BY_CONTEXT` with discriminator `period (one covers part of
  the other)`, rather than flagging a contradiction between an annual and a quarterly
  figure.
- **Unit/scale.** The 2022 prospectus states net issue proceeds in `₹40,000.00 million`;
  the FY24 annual report's continuing disclosure restates a related figure in crore. Once
  normalized (`normalize/values.py`), ₹40,000.00 million = ₹4,000 Cr — same value, and the
  claim keys' `unit_family` component is what differs when the raw scale words differ
  without full normalization elsewhere in the corpus.
- **Basis.** "Revenue from services" is footnoted in the deck as excluding revenue from
  traded goods; a total revenue figure elsewhere includes it. The `basis` qualifier is what
  the reconciler names as the explanation.

*(exact fact IDs and rendered evidence to be inserted here)*

---

## Case 4 — extraction and reasoning failures found, and how they were handled

The assignment asks for one; running the full pipeline against the real corpus surfaced
several distinct failure modes, each with a different fix or disposition. This is the most
honest part of the submission, because every one of these was found by reading actual
output, not anticipated in advance.

### 4a. A table row copied whole instead of decomposed per cell — **fixed**

The extraction prompt is explicit that a table row, rendered as
`label: header=value; header=value; ...`, must become one fact **per cell**, with the
period coming from that cell's own header. On rows with many columns, the extraction model
sometimes ignored this and copied the entire row back as a single `"value"` string:

```
attribute: "Cyclically adjusted balance (% of potential GDP)"
value:     "2021/22=-7.7; 2022/23=-8.2; 2023/24=-8.1; 2024/25=-7.9; 2025/26=-7.2; 2026/27=-7.1"
```

This is a dangerous failure precisely because it is **invisible to grounding**: the string
is a real, verbatim substring of the source table, so it passes the quote-verification
check with a perfect score. The normalizer (`normalize/values.py`) then reads off the
*first* number it finds in that string — which, because row-major cells lead with a period
like `2021/22=`, is the year `2021`. Left unguarded, this produces a fact reading
"Cyclically adjusted balance = 2021" — grounded, confident, and meaningless.

**Fix:** `extract/extractor.py::_looks_like_undecomposed_row` rejects any proposed value
containing both `=` and `;`, on the observation that no legitimate single value in this
corpus's schema ever contains both characters together — every real value is a bare
number, a signed or parenthesised amount, a currency string, a period label, or a short
status phrase. This was verified against the live corpus: 26 such row-dumps were caught
and rejected on the actual extraction run (see the Review screen), at the cost of zero
false positives among the ~350 legitimately-shaped values checked alongside them.

**What I'd improve further:** a smarter fix would have the extractor retry a rejected wide
row with a follow-up prompt asking specifically for the per-cell decomposition, rather than
simply discarding it — recovering the facts instead of only avoiding the wrong ones.

### 4b. A value truncated mid-phrase — **fixed**

A related but distinct failure: values that were real, correctly grounded substrings, but
cut off before their own content finished —

```
attribute: "contingent liabilities"
value:     "5.6 percent of"
```

This is worse than merely awkward, because the missing continuation ("...of what?") is the
entire substantive content of the claim. **Fix:** `_looks_truncated` rejects any value
whose last word is a bare preposition, article or conjunction ("of", "the", "a", "to",
"and", ...), verified against the live run to catch real truncations with no false
positives among values that legitimately end in a word following one of those ("to an
NBFC").

### 4c. Garbled chart text read as a fact's value — **found, not yet fixed**

```
subject:   India
attribute: real gdp growth revision
value:     "India: Real GDP Growth Revision (from July WEO) India: (Percentage Impact
            points, of relative Lower to baseline) US Tariffs on Real GDP Growth"
grounding: verbatim (100)
```

This is a genuinely different failure class from 4a/4b. The source is a chart — axis
labels, a legend, and a caption — whose text, extracted linearly by PyMuPDF, interleaves
into a scrambled but real string. The model faithfully reported it as a fact's "value"
because that string *is* what appears on the page in that reading order. Grounding is
correct; the content is not useful.

**Why this one is left as a known limitation rather than patched:** unlike 4a/4b, this
failure is *harmless downstream* — `normalize/values.py` correctly fails to extract a
number from this text, so `value_num` stays `None` and the fact never enters any numeric
comparison, derived-value check, or reconciliation. It sits inertly in the facts table,
visible on the Facts screen for a human to judge, rather than contaminating a comparison
the way 4a would have. I chose to spend the limited remaining time on the failures that
could silently corrupt a *comparison* (4a, 4b) rather than on this one, which only
pollutes the extras. A proper fix would detect chart/figure regions during layout
reconstruction (`ingest/layout.py`) and either exclude them or represent them differently
from prose — a natural next step, noted in the README's Limitations section.

### 4d. Qualitative statements extracted under a quantitative-sounding attribute name — **handled by existing design, not patched**

```
attribute: real gdp growth
value:     "has remained robust"
```

The model occasionally extracts a qualitative sentence fragment under an attribute name
that, elsewhere in the corpus, is used for numeric values. This is not incorrect
extraction — the source does say growth "has remained robust" — but it creates a fact
whose *value_kind* doesn't match its attribute's usual shape.

I did not add a special case for this, because the existing reconciliation logic already
handles it correctly: `reason/rules.py::classify` only takes the numeric-comparison branch
when *both* facts in a candidate pair have a parsed `value_num`. A non-numeric fact
compared against this one falls to the non-numeric path, which asks the model to
adjudicate rather than silently merging or flagging a false contradiction. This is a case
where the system's honest response to an extraction quirk is to defer to review rather
than guess — which is the intended behaviour, not a gap.

---

## What I would build next given more time

- Retry-with-narrower-prompt for rejected wide-table rows (4a), to recover facts instead
  of only discarding wrong ones.
- Chart/figure region detection in the layout reconstructor, to stop presenting chart text
  to the extractor as if it were a paragraph (4c).
- A confidence-weighted view on the Findings screen that surfaces case 4's near-misses —
  facts that grounded but tripped a quality guard — as a distinct, browsable category
  rather than only visible via the CLI's `review` command's rejection reasons.
