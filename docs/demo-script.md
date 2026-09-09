# 3-minute demo — shot list

Everything below is verified against the committed sample database. Every search term and
command was run and returns exactly what the script says it does.

**Spoken words are the hard budget**, not the number of things to click. The script is 479
words: about 2:54 at a brisk demo pace (~165 wpm). That leaves almost nothing for
navigation pauses, so it is written to be cut. Drop, in this order: the second half of
segment 1's sentence (everything after "Indian economy"), segment 2's third sentence, then
segment 8's first two sentences. The four cases and the evidence shot are what's being
marked; everything else is padding you can lose without losing a requirement.

## Film segment 2 first, on its own, then reset

Segment 2 uploads a PDF, and a completed upload changes every number the rest of the
script points at -- the stat strip, `7,750` on the Facts tab, the rejection counts, the
`4,579`. Filming in order and letting the upload finish in the background will quietly
invalidate segments 6, 7 and 8 while you are still talking.

So: **shoot segment 2 as its own take, then reset the database and restart the server
before shooting anything else.** Drop it into position 2 when you edit. Everything after
that is read-only, so it can be filmed in one continuous pass.

## Before you hit record

```bash
cd crosscheck
cp samples/crosscheck.sample.db data/crosscheck.db      # the real, populated run
python -m uvicorn crosscheck.api.app:app --port 8077
```

Run those same two commands again -- the copy and a server restart -- between the
segment 2 take and the rest.

- Open <http://localhost:8077> — land on **Documents**. The header reads your live
  `.env`, so it will name whichever models you have configured.
- Open a second terminal, `cd crosscheck`, ready for the CLI shots.
- Have `samples/delhivery-annual-report-fy25-excerpt.pdf` open in your file picker,
  ready to drag. 15 pages, 19 blocks — enough that the counter visibly moves. Measured
  end to end it took 33 minutes, almost all of it waiting out Cerebras' 5-requests-per-minute
  free-tier limit, so do not plan to film it finishing. It is Delhivery's **FY25** annual report, a document
  the corpus has never seen; the corpus holds the **FY24** one. See segment 2 for why that
  matters.
- Browser zoom ~110%. Close other tabs.

---

## 1 · Open — 0:00–0:15

**Screen:** Documents tab. The header names both models; the stat strip reads
6 · 7,750 · 3,618 · 4,579 · 1,382; the six documents are listed below it.

> "This is CrossCheck. It reads PDFs, ties every fact to the exact words that support it,
> and works out when facts agree, disagree, or only *look* like they disagree. Six
> documents — Delhivery filings and three reports on the Indian economy — seven and a half
> thousand facts, and three and a half thousand attributes it discovered on its own."

*(The two lines top right are the two model roles: extraction and reasoning are configured
separately, and run on different providers here. Don't stop to explain it — segment 8
picks it up if you have room.)*

---

## 2 · A PDF going in — 0:15–0:35

**Screen:** Drag `delhivery-annual-report-fy25-excerpt.pdf` onto the drop zone. A progress
bar appears under it with the filename, the stage — `ingesting` → `identifying` →
`extracting` — and an `n / N blocks` chip that ticks up towards 19.

> "Drop in a PDF and the whole thing runs — layout rebuilt from word positions, facts
> extracted, each one grounded, then compared against everything already known. This is
> Delhivery's FY25 report; the corpus only knows FY24. It takes a while on a free tier, so
> here's one already built."

**Cut** once the counter reaches 3 or 4 of 19. Don't wait for it.

*Why this file and not any PDF: it is genuinely unseen, and it overlaps. The narration's
last clause is what turns this shot from a progress bar into a claim about the system — and
the claim is verified. Running the full pipeline on this file against a copy of the sample
database produced:*

| | |
|---|---|
| facts extracted | 334 grounded from 377 proposed (89%) |
| identified as | "Annual Report 2024-25", Delhivery Limited — inferred, not configured |
| new relations to the existing corpus | **358**, all by rule, zero model calls |
| of which | 314 reconciled-by-context, 41 corroborations, 3 derived |

*The cleanest one: `total income (INR)` = **₹85,942.34 million, FY2023-24** appears in the
FY24 annual report already in the corpus **and** in the FY25 report's comparative column.
Two independently ingested documents, same figure, matched on a claim key rather than on
text. Freight costs (₹59,707.49 M) and employee benefits (₹14,367.70 M) corroborate the
same way.*

*It also self-contradicts usefully: `revenue from operations` for FY2024-25 is ₹82,524.47 M
standalone and ₹89,319.01 M consolidated — same measure, same period, reconciled by scope,
from a single fresh document.*

**If you have time before filming**, run the upload to completion once, then film segment 2
against the finished result and add ten seconds showing one of those corroborations on the
Findings tab. It is the strongest evidence of incremental ingest in the whole demo. If you
don't, the five-second progress shot plus the narration is enough.

---

## 3 · Grounding — the core claim — 0:35–1:05

**Screen:** **Facts** tab → type `8,142` in search (the chip on the right reads
`11 matching`) → click the first row, `revenue from services · ₹8,142 Cr`. The panel opens
below the table with the quote, three relationships, and the highlighted page image.

*Point at the **Grounding** column as you say the second sentence — every row reads
`verbatim`.*

> "Every fact survived two checks: its quote has to appear in the source text, and the
> number has to appear inside that quote. That second one matters — it catches a real quote
> paired with a number lifted from the next row of a table. Anything that fails is rejected,
> not stored. And here's the page it came from, evidence boxed."

*(Let the highlighted slide image sit on screen for a beat — it's the strongest single
image in the demo.)*

---

## 4 · Case 3 — an apparent contradiction, explained — 1:05–1:30

**Screen:** Terminal.

```bash
python -m crosscheck.cli relations --type RECONCILED_BY_CONTEXT --query "against Q4 FY2023-24" --limit 1
```

> "₹8,142 crore and ₹2,076 crore, same company, same measure. That looks like a flat
> contradiction. It isn't — one is the full year, the other is the fourth quarter inside
> it. The system resolves both periods to real date intervals, sees that one contains the
> other, and says so. No model call: a rule decided this."

---

## 5 · Case 1 — corroboration between facts that aren't the same claim — 1:30–1:55

**Screen:** Terminal.

```bash
python -m crosscheck.cli relations --type DERIVED_CONSISTENT --query "2,076" --limit 1
```

> "Here's the more interesting kind of agreement. EBITDA of 46 crore, revenue of 2,076
> crore, and a stated margin of 2.2 percent. No two of those are the same claim, so nothing
> matches on keys — but 46 divided by 2,076 is 2.22 percent, which is the stated margin.
> Nothing about EBITDA is hard-coded; it looks for arithmetic that holds between facts
> sharing a subject and a period."

---

## 6 · Case 2 — a genuine disagreement — 1:55–2:25

**Screen:** Back to the browser, **Facts** tab. Search `real gdp grew` → `1 matching`,
`real gdp growth (percent) · 6.5 percent`, qualifier chip `FY2024-25`, source
International Monetary Fund. Clear it and search `advance estimates` → `5 matching`; the
first row is the same attribute at `6.4 per cent`, same `FY2024-25` chip, source
*India Economic Survey 2024-25*.

*The two `FY2024-25` chips are the shot. Same attribute, same period, different numbers —
put them side by side by leaving the first search on screen a beat before clearing it.*

> "Real GDP growth, same Indian fiscal year. The IMF says 6.5 percent. The Economic Survey
> says 6.4, and calls it a first advance estimate. Same measure, same twelve months,
> different numbers. Being straight about this one: the system files it under 'reconciled'
> because the extractor put a bogus scope tag on one side — a rule explained away a real
> difference. That's documented as a known failure, not hidden."

---

## 7 · Case 4 — what it gets wrong — 2:25–2:50

**Screen:** **Review** tab. The top table is *Why extractions were rejected* — reason,
what went wrong, count. The three `ungrounded` rows read 811 / 341 / 109, and the middle
one is the line to point at: *value not present in the quoted evidence*.

> "Which is the point of this screen. Fourteen hundred proposed facts were refused — eight
> hundred where the quote wasn't in the source at all, and three hundred and forty where
> the quote was real but the number wasn't in it. That second kind is the dangerous one.
> Reading this screen is how most of this got fixed — five real bugs, including one that
> silently merged 'revenue' with 'percentage of revenue' across the whole vocabulary."

---

## 8 · Close — 2:50–3:00

**Screen:** **Findings** tab. Open the type dropdown so the per-type counts show —
`All types (4,579)`, then CONTRADICTS, RECONCILED BY CONTEXT, CORROBORATES,
DERIVED CONSISTENT with their own counts. Close it; every card carries a
`decided by rule` or `decided by llm` note on its top right.

> "Forty-five hundred relationships. Rules settle what rules can — periods, units,
> arithmetic — and the model is asked only where judgement is needed. Every card records
> which. The whole run is committed, so you can browse it without a key."

---

## If you fumble

- **The stat strip reads 0s, or the header says no model configured** — you are pointing
  at the wrong database or an empty `.env`. Copy the sample DB over `data/crosscheck.db`
  and restart the server; the header and the counts both read live config.
- **Search returns nothing** — same cause: check the sample DB actually landed.
- **Evidence image doesn't load** — it needs `starter-datasets/` present; it's committed, so
  just confirm you're running from the repo root.
- **Upload seems stuck** — expected, and measured: 19 blocks took 33 minutes, 14 of those
  calls sat waiting on the free tier's rate limit. The shot needs five seconds of movement,
  not a finished run. Cut away.

## Two things worth saying if you have room

- It runs on a free API tier — the table extraction for the whole corpus cost nothing.
- Nothing is specific to these documents: no hard-coded facts, filenames, or schemas.
