# CrossCheck — Implementation Plan

A fact knowledge layer: extract grounded facts from PDFs, link every fact to verified
evidence, and explain when facts corroborate, contradict, or only *appear* to conflict.

---

## 1. The core idea

Most "fact extraction + graph" projects fail the interesting half of this assignment: they
put numbers in a graph and call two different numbers a contradiction. The actual difficulty
is that **two numbers are only comparable once you agree on what claim they are making.**

So the system is built around one central object, the **claim key**:

```
claim_key = (subject, attribute, period, scope, basis, unit_family)
```

Everything follows from it:

| Situation | Meaning | Case |
|---|---|---|
| claim keys equal, values agree within tolerance | **CORROBORATES** | 1 |
| claim keys equal, values disagree | **CONTRADICTS** (or supersession / vintage revision) | 2 |
| subject + attribute equal, but a *key component differs* | **RECONCILED_BY_CONTEXT** — and the differing component **is** the explanation | 3 |
| values are consistent through arithmetic (x, y, x/y) or (v₀, v₁, yoy%) | **CORROBORATES**, expressed differently | 1 |

Nothing about this is document-specific. The subjects, attributes, periods and units are all
discovered from the documents themselves; only the *comparison logic* is code.

The second design commitment is **grounding is verified, not asserted**. An LLM that says
"page 12 says X" is not evidence. Every extracted fact must survive a verbatim span check
against the real page text, and its numeric value must appear inside the matched span, or the
fact is rejected into a review queue. Hallucinated facts do not enter the knowledge layer.

---

## 2. Architecture

```
PDF ──► ingest ──► blocks ──► [prefilter] ──► LLM extract ──► GROUND ──► normalize
                                                                │  reject
                                                                ▼
                                                          review queue
                                                                │
                       ┌────────────────────────────────────────┘
                       ▼
       attribute canonicalisation (evolving schema)
                       │
                       ▼
        claim clustering  ──►  rule adjudicator  ──►  LLM adjudicator (only where rules
                                (deterministic)        are ambiguous) ──► relations
                       │
                       ▼
                SQLite  ◄──►  FastAPI  ◄──►  web UI
```

Stack: **Python 3.11 · FastAPI · SQLite (FTS5) · PyMuPDF · vanilla JS UI**
(no build step — `pip install` then `uvicorn` is the entire setup).

Models: planned as Anthropic Haiku/Sonnet, below. What actually shipped is
**`gpt-oss-120b` on Cerebras's free tier** for bulk per-block extraction — measured
against Haiku, gpt-5-nano and gpt-4.1-mini on this corpus, it won outright (see the
`AI tools used` section of `README.md`) — and **`gpt-4.1-mini`** for pairwise
adjudication and attribute canonicalisation. Provider is behind a thin interface
(`crosscheck/config.py`), so any OpenAI-compatible endpoint or the Anthropic Messages
API works as a straight config change; `.env.example` documents both.

*(Original plan, kept for the record: Haiku 4.5 for bulk extraction — cheap, parallel,
high JSON reliability — and Sonnet 5 for adjudication, needing the reasoning.)*

---

## 3. Pipeline stages

### 3.1 Ingest
- PyMuPDF: per-page text, word boxes, and `find_tables()` for grid regions.
- Blocks = paragraphs and table regions, each carrying `(page_no, char_span, bbox, section_path)`.
  Section path comes from font-size/bold heuristics so a fact knows it lives under
  "Consolidated Financial Statements → Note 22".
- **Tables are serialised to markdown with headers repeated per row**, because a bare cell
  `9.2` is useless to an extractor that cannot see the column header `2023/24`.
- Footnotes on the same page are appended to each block's context — this corpus qualifies
  half its numbers in footnotes (`"excluding revenue from traded goods"`, `"as per RedSeer
  report basis FY21 revenue"`).
- Document metadata (title, publisher, as-of date) inferred once from page 1 by the LLM;
  this becomes the *vintage* used later to distinguish "revised estimate" from "contradiction".
- Optional OCR fallback (`pytesseract`) when a page's text density is near zero, so scanned
  test PDFs degrade rather than fail.

### 3.2 Extraction
One LLM call per block, structured output enforced via tool-use schema, temperature 0.
The prompt's hard rules:
- extract only what is *stated*; never infer, compute, or carry over from another block;
- `evidence_quote` must be copied **verbatim** and must contain the value;
- the attribute is a bare noun phrase — **period, scope and basis go in qualifiers, never
  baked into the attribute name** (this is what makes claim keys comparable later);
- unknown qualifier → `null`, never a guess;
- semantic facts count too: director appointed/resigned, registered office address, auditor,
  listing date, ratings — not just numbers.

Prefilter: blocks with no digits and no entity-verb pattern (disclaimers, safe-harbour
boilerplate, page furniture) skip the LLM entirely — roughly a third of this corpus.

### 3.3 Grounding (the anti-hallucination gate)
1. Normalise (unicode, ligatures, whitespace) and exact-substring the quote against page text.
2. Miss → `rapidfuzz` partial alignment ≥ 90, recovering the char span.
3. Miss → **reject** the fact to `rejected_facts` with a reason.
4. Assert the raw value string occurs inside the matched span — catches the common failure
   where the quote is genuine but the number was transposed from a neighbouring row.
5. Map char span → word boxes → per-line rects. Page PNG with the highlight is rendered on
   demand and cached, so the UI shows the fact circled on the actual page.

### 3.4 Normalisation (deterministic, unit-tested)
- **Values** — `₹8,142 Cr`, `₹40,000.00 million`, `Rs. (452 Cr)`, `1.4 Mn Tons`, `(6.3%)`,
  `18,074`, `73 bps` → `(kind, number_in_base_unit, unit, scale, raw)`. Parentheses are
  negative. The crore/million/lakh mix inside a single corpus is a real trap here: the
  prospectus quotes `₹40,000.00 million` where the earnings deck quotes `₹8,142 Cr`.
- **Periods** — `FY24`, `FY 2023-24`, `FY2024/25`, `2024-25`, `Q4 FY24`, `9M FY24`, `CY2024`,
  `as of March 31, 2024` → `(start, end, granularity, label)` on an April–March fiscal year.
  This is load-bearing: the Survey's `FY25`, RBI's `2024-25` and the IMF's `FY2024/25` are the
  same interval, and failing to see that manufactures fake contradictions across publishers.
- **Entities** — alias resolution (`Delhivery Limited` / `the Company` / `Delhivery`),
  address canonicalisation, person-name matching for the director cases.

### 3.5 Evolving schema
Attribute strings arrive as free text (`revenue from services`, `service revenue`,
`revenue from operations excluding traded goods`). A canonicalisation pass embeds/batches new
attribute strings and asks the LLM to either merge them into an existing canonical attribute
or register a new one, recording aliases and the expected value kind and unit family.

The vocabulary is a table that **grows as documents arrive** — never a fixed enum — and the UI
has a Schema page showing it growing, which is the "dynamically evolving schema" brownie point.

### 3.6 Reconciliation — rules first, LLM only where genuinely ambiguous
Deterministic layer decides everything it can and produces the *label*; the LLM only writes
explanations and resolves the genuinely hard cases. This keeps the system auditable — every
relation records whether a rule or the model decided it.

```
same claim_key + values agree (kind-aware tolerance, rounding-aware) → CORROBORATES        [rule]
same claim_key + values disagree                                     → LLM adjudicates
      → CONTRADICTS | SUPERSEDES (later vintage revises earlier) | RECONCILED_BY_CONTEXT
subject+attribute match, exactly one key component differs           → RECONCILED_BY_CONTEXT [rule]
      discriminator ∈ {period, scope, basis, unit, geography, vintage}
```

Plus a generic **derived-value checker** that finds corroboration between differently
expressed facts, with no hard-coded formulas:
- ratio triples: any `(x, y, r)` sharing subject+period where the ratio attribute names a
  margin/share/percentage-of → check `r ≈ x/y` (EBITDA ₹127 Cr ÷ revenue ₹8,142 Cr ≈ 1.6%);
- growth triples: `(v_prev, v_now, yoy%)` → check the stated growth against the two levels.

The LLM adjudicator receives both facts, both full quotes, both documents' metadata and
publication dates, the normalised values, and which key components differ. It returns
`{label, discriminator, explanation, confidence}` with the explanation required to cite both
quotes. Temperature 0.

### 3.7 Storage
SQLite, single file, zero setup. Tables: `documents, pages, blocks, facts, attributes,
entities, clusters, relations, rejected_facts, extraction_cache`, plus `facts_fts` (FTS5) for
search. Relations are edges in a table — a graph when you want one, without a graph database
being mistaken for the contribution.

---

## 4. API and UI

```
POST /api/documents                        upload PDF → {doc_id, job_id}
GET  /api/jobs/{id}/stream                 SSE progress
GET  /api/documents                        list + per-doc stats
GET  /api/facts?q=&doc=&attr=&period=&min_conf=
GET  /api/facts/{id}                       fact + evidence + its relations
GET  /api/documents/{id}/pages/{n}.png?fact=  page image with highlight
GET  /api/relations?type=CONTRADICTS
GET  /api/clusters                         claim clusters
GET  /api/schema                           discovered attribute vocabulary
GET  /api/review                           rejected + low-confidence facts
```

UI (five screens, vanilla JS):
1. **Documents** — drag-drop upload, live progress, per-doc counts.
2. **Facts** — searchable/filterable table; selecting a fact opens the evidence pane with the
   highlighted page image.
3. **Findings** — the main screen. Cards grouped by relation type: two facts side by side,
   both verbatim quotes, both highlighted page crops, a discriminator chip (`period`,
   `vintage`, `scope`, `unit`), and the written explanation.
4. **Schema** — the attribute vocabulary and its aliases, with growth over ingests.
5. **Review** — rejected extractions and low-confidence facts (this is case 4, in the product).

---

## 5. The four required cases

Sourced from the corpus, not invented:

1. **Corroboration, expressed differently** — FY24 EBITDA ₹127 Cr and 1.6% EBITDA margin in the
   earnings deck vs revenue from services ₹8,142 Cr; the derived-value checker confirms
   127/8142 ≈ 1.6%. Cross-document: FY24 figures repeated in the annual report in a different
   unit and phrasing.
2. **Genuine/likely contradiction** — same claim key, incompatible values. Strongest candidates
   are real GDP growth for the same fiscal year across the Economic Survey, RBI and IMF, and
   board composition between the 2022 prospectus and the FY24 annual report.
3. **Apparent contradiction, explained** — `₹8,142 Cr` vs `₹2,076 Cr` revenue (FY24 vs Q4 FY24 —
   *period*); `₹40,000.00 million` vs `₹4,000 Cr` (*unit*); revenue from services vs total
   revenue including traded goods (*basis*); a director "active" in 2022 and "resigned" in FY24
   (*as-of date*).
4. **Failure** — the review queue plus a written post-mortem of at least one real failure found
   during development, with the mitigation shipped and the deeper fix described.

`docs/four-cases.md` will hold each case with its evidence, the system's reasoning, and screenshots.

---

## 6. Brownie points, addressed by construction

- **Large PDFs** — block-level prefilter, async concurrency pool with backoff, per-page work so
  memory is flat, and page images rendered lazily.
- **Many PDFs** — a new document is compared against cluster representatives, not all existing
  facts, so linking is O(n·k) rather than O(n²).
- **Evolving schema** — §3.5; the attribute table is data, not an enum.
- **Incremental ingest** — content-addressed `extraction_cache` keyed on
  `sha256(block_text + prompt_version + model)`, so re-uploads and re-runs are near-free, and
  only *new* candidate pairs get adjudicated. Nothing is rebuilt.

---

## 7. Repository layout

```
crosscheck/
  README.md              setup · demo video · approach · limitations
  PLAN.md                this file
  docs/four-cases.md     the four required cases, with evidence
  .env.example           ANTHROPIC_API_KEY=...        (no secrets committed)
  requirements.txt
  crosscheck/
    config.py db.py models.py cli.py
    ingest/    pdf.py blocks.py tables.py meta.py
    extract/   prompts.py llm.py extractor.py cache.py
    normalize/ values.py periods.py units.py entities.py
    ground/    verify.py bbox.py render.py
    link/      attributes.py cluster.py
    reason/    rules.py derived.py adjudicate.py
    api/       app.py routes.py schemas.py
    web/       index.html app.js styles.css
  tests/                 unit tests on the deterministic core
  samples/               committed run output + screenshots (evaluable without an API key)
  starter-datasets/      the provided PDFs, so the repo is self-contained
```

---

## 8. Milestones

| # | Deliverable |
|---|---|
| M0 | Repo, git init, config, SQLite schema, PDF ingest, `cli.py ingest` dumps blocks |
| M1 | `normalize/` values + periods + units, with unit tests (the deterministic core, done first) |
| M2 | LLM extraction + grounding verification + persistence + extraction cache |
| M3 | bbox mapping and highlighted page rendering |
| M4 | Attribute canonicalisation + claim clustering |
| M5 | Rule adjudicator, derived-value checker, LLM adjudicator → relations |
| M6 | FastAPI + the five UI screens |
| M7 | Full 6-document run; curate and write up the four cases |
| M8 | Perf/incremental polish, `samples/` output, README, 3-minute demo video |

Git commits land per milestone with meaningful messages, as the assignment asks.

---

## 9. Trade-offs taken deliberately

- **Rules decide labels, the LLM explains them.** Slightly less flexible than pure-LLM
  adjudication, but every relation is auditable and reproducible, and disagreement between the
  rule and the model is itself a signal worth surfacing.
- **No graph database.** Relations are a table. The assignment explicitly warns that a graph is
  not the solution, and SQLite keeps setup to one `pip install`.
- **Extraction is per-block, not whole-document.** Loses some long-range context (recovered
  partially via section path + footnotes), but makes grounding exact, work parallel, caching
  possible, and large PDFs tractable.
- **No sentence-transformer dependency by default.** Blocking uses TF-IDF character n-grams plus
  LLM attribute canonicalisation instead of a 200 MB torch install; an optional extra enables
  embeddings for better recall.
- **Facts are per-document assertions, never merged into a single "truth".** The system reports
  who claims what and how the claims relate; it does not adjudicate reality. That is the honest
  behaviour for contradicting institutional sources.

---

## 10. Cost and runtime estimate

~6 documents × ~100 pages ≈ 2,000 post-prefilter blocks. Haiku extraction plus a few hundred
Sonnet adjudications lands in the low single-digit dollars for a full corpus run, a few minutes
wall clock at concurrency 8. Cached re-runs are free. `samples/` is committed so the submission
can be evaluated without spending anything.
