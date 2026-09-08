# CrossCheck — a fact knowledge layer

Extracts facts from PDFs, links every fact to verified evidence in its source document, and
explains when facts corroborate, contradict, or only *appear* to conflict.

Upload a PDF through the UI or the API and the whole layer runs against it: layout
reconstruction, extraction, grounding, vocabulary consolidation, then reconciliation against
everything already known. Nothing is specific to the starter documents — no hard-coded facts,
filenames, schemas or per-document rules.

---

## Setup and run instructions

Requires Python 3.11+.

```bash
git clone <this repo> && cd crosscheck
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                              # then add an API key
```

Put a key in `.env`. Groq's free tier is enough to run the whole thing:

```
CROSSCHECK_EXTRACT_PROVIDER=openai
CROSSCHECK_EXTRACT_BASE_URL=https://api.groq.com/openai/v1
CROSSCHECK_EXTRACT_API_KEY=gsk_...
CROSSCHECK_EXTRACT_MODEL=openai/gpt-oss-120b
CROSSCHECK_EXTRACT_REASONING_EFFORT=low
```

Start the server:

```bash
python -m uvicorn crosscheck.api.app:app --port 8077
```

Open <http://localhost:8077> and drop in a PDF.

### Or from the command line

```bash
python -m crosscheck.cli ingest "starter-datasets/delhivery/*.pdf"
python -m crosscheck.cli extract          # all documents
python -m crosscheck.cli link             # consolidate the attribute vocabulary
python -m crosscheck.cli reconcile        # find relationships

python -m crosscheck.cli relations --type CONTRADICTS
python -m crosscheck.cli facts --query "revenue"
python -m crosscheck.cli evidence 42      # render a fact's evidence on its page
python -m crosscheck.cli review           # what the system refused to believe
python -m crosscheck.cli schema           # the discovered attribute vocabulary
```

`link` and `reconcile` accept `--no-llm` to run on rules alone, with no API calls at all.
Tests need no key: `python -m pytest -q`.

Nothing is committed that holds a credential; `.env` is gitignored.

---

## Video demo

*(link)*

---

## Approach

### The problem is comparison, not extraction

Pulling numbers out of a PDF is the easy half. The hard half is that **two numbers are only
comparable once you agree what claim each is making.** ₹8,142 Cr and ₹2,076 Cr look like a
flat contradiction until you notice one is a year and the other its fourth quarter.

So the system is built on one object, the **claim key**:

```
(subject, attribute, period, scope, basis, unit_family)
```

| Claim keys | Values | Relation |
|---|---|---|
| identical | agree | **CORROBORATES** |
| identical | disagree | **CONTRADICTS**, or **SUPERSEDES** if a later document revised an earlier estimate |
| differ in one component | disagree | **RECONCILED_BY_CONTEXT** — and the differing component *is* the explanation |

Case 3 falls out of the data model rather than needing special handling. This is also why the
extraction prompt is built around one rule: **qualifiers never go in the attribute name.** An
attribute of `"FY24 consolidated revenue in crore"` can only ever match itself.

Keys are built from the *interval* a period denotes, never its label, so the Economic
Survey's `FY25`, the RBI's `2024-25` and the IMF's `FY2024/25` collide as one claim. Getting
this wrong invents contradictions between sources that agree and hides the real ones.

### Grounding is verified, not asserted

A model saying "page 12 states X" is not evidence. Every proposed fact must survive two
checks against the exact text the model was shown:

1. its quote must be **found in the source**, verbatim or by high-threshold fuzzy alignment;
2. its **value must appear inside that quote**.

The second check is the one that earns its keep: it catches a genuine quote paired with a
number lifted from a neighbouring table row — a failure that is indistinguishable from a real
fact afterwards. Anything that fails lands in a **review queue** with a reason rather than in
a log, because those failures are evidence about the extractor and belong in the product.

Verified evidence is then located on the page and rendered with a highlight. Prose is matched
by token sequence; a table row cannot be, because the quote is a row *this system
synthesised*, so the row label is found and the value located within its band.

### Layout is reconstructed from geometry

PyMuPDF's own structure extraction was silently wrong on this corpus in ways that produce
confident, evidence-backed, false facts. The RBI report yields one block per *line* in two
columns, so naive blocking gives fragments like `"headline inflation"` / `"eased by 73 bps to
4.6 per cent"`. `find_tables` shreds borderless financial tables into `"Real GDP (a"` |
`"t market price"` | `")"` with values offset from their headers.

So layout is rebuilt from word boxes. The key observation is that **financial tables are
decimal-aligned**: the x-centres of numeric tokens cluster tightly even when no ruling line
exists and the row labels are ragged. Columns come from those clusters, and every value is
emitted bound to its own header:

```
Real GDP (at market prices): 2021/22=9.7; 2022/23=7.6; 2023/24=9.2; 2024/25=6.5
```

Slides are treated as a separate medium: rows are split at wide gaps and clustered by
x-centre, so each figure stays with its own caption instead of arriving on one line with the
captions on the next.

### Rules decide, the model explains

The claim key does the hard part, so the reconciliation rules are small and deterministic.
The model is asked only for what rules genuinely cannot settle — chiefly whether a
disagreement is a contradiction or a later vintage revising an earlier estimate, which means
reading what each source claims about firmness. **Every relation records whether a rule or the
model decided it**, so the reasoning is auditable rather than a black box.

A **derived-value checker** finds corroboration between facts that are not the same claim at
all: a stated margin against the amounts that imply it, a stated growth rate against the two
levels it spans. No formula is hard-coded; it looks for arithmetic that holds among facts
sharing a subject and period, and requires the attribute names to overlap before believing
it. Without that guard it is numerology — among a few dozen figures sharing a period, some
pair divides into some percentage by chance.

### The schema evolves

The attribute vocabulary is a table, not an enum. It starts empty and grows as documents
introduce measures nobody anticipated. Fuzzy grouping does the free work and pins each group
to a unit family; the model names the groups and splits any that should not have merged. The
dangerous merge is a level with a rate of change — `revenue` with `revenue growth`, `EBITDA`
with `EBITDA margin` — because the strings look nearly identical and the claims are
unrelated, so the prompt is built around refusing it.

### Engineering decisions and trade-offs

- **SQLite, not a graph database.** Relations are rows. The assignment is explicit that a
  graph is not the contribution, and this keeps setup to one `pip install`.
- **Two model roles, configured separately.** Extraction is ~700 high-volume, low-judgement
  calls where a free tier is right and the grounding gate catches what a weaker model gets
  wrong. Reconciliation is a few hundred calls where judgement decides the output.
- **Rules over prose.** A rule-decided relation gets its explanation written from the recorded
  facts, not by a model: the rule already knows why it decided, so paying for prose would add
  cost and a chance to be wrong.
- **Refusing to represent what cannot be represented honestly.** Tables whose columns cannot
  be named, or whose cells hold two numbers, are dropped back to prose. A number that is real
  with a binding that is invented is the worst failure mode — it grounds perfectly and means
  nothing.
- **No embedding model.** Blocking uses fuzzy string similarity plus LLM canonicalisation
  rather than a 200 MB torch install. Cheaper to run and to install; costs some recall on
  attributes that share no words.
- **Facts are per-document assertions, never merged into a single truth.** The system reports
  who claims what and how the claims relate. It does not adjudicate reality, which is the
  honest behaviour when institutional sources disagree.

### Performance and incrementality

- **Caching is content-addressed** on block text + prompt version + model, so re-ingesting a
  document, adding one, or resuming an interrupted run costs nothing for work already done.
- **Blocks are packed** to ~5,000 characters, which cut the corpus from 15,334 extraction
  units to ~660 while giving each call more of the context a number needs.
- **Linking is incremental**: a new document is compared against the clusters its facts join,
  not against the whole corpus, and only genuinely new attributes reach the vocabulary pass.
- **A sliding-window token bucket** paces requests under the provider's limit. Free tiers
  meter tokens, not requests, and discovering that by hitting 429s wastes most of the budget:
  reserving before sending took measured throughput from 0.6 to 2.6 blocks per minute on the
  same account.

### AI tools used

Claude Code (Opus) for implementation throughout. Model choice for the pipeline itself was
settled by measurement rather than assumption — see *Limitations* below.

---

## Limitations and next steps

*(completed after the full corpus run — see `docs/four-cases.md`)*

---

## Additional notes

*(to follow)*
