# Sample output

`crosscheck.sample.db` is the real, fully-populated database from the run that produced this
submission: all six starter documents ingested and extracted (7,750 grounded facts), the
attribute vocabulary consolidated (3,618 canonical attributes), and reconciliation run
against the whole corpus (4,579 relations — corroborations, contradictions, context-explained
reconciliations, and derived-value matches). It costs nothing to explore and needs no API
key.

## To browse it

```bash
cp samples/crosscheck.sample.db data/crosscheck.db
pip install -r requirements.txt
python -m uvicorn crosscheck.api.app:app --port 8077
```

Open <http://localhost:8077>. Every screen — Documents, Facts, Findings, Schema, Review —
shows this real data. Evidence images (the highlighted source page behind any fact) work too:
`document_path()` falls back to the PDFs in `starter-datasets/`, which are committed
alongside this database, when the original absolute upload path (baked in on the machine
that ran the extraction) does not exist on yours.

## Or from the command line, no server needed

```bash
cp samples/crosscheck.sample.db data/crosscheck.db
python -m crosscheck.cli relations --type CONTRADICTS
python -m crosscheck.cli relations --type RECONCILED_BY_CONTEXT --limit 5
python -m crosscheck.cli facts --query "revenue from services"
python -m crosscheck.cli schema --limit 20
python -m crosscheck.cli review
```

## An unseen document, to try ingestion yourself

`delhivery-annual-report-fy25-excerpt.pdf` is a 15-page excerpt of Delhivery's **FY25**
annual report ([source](https://www.delhivery.com/uploads/2025/08/Annual_Report_FY25.pdf)),
kept here because the corpus above contains the **FY24** one. Drop it into the UI, or
`python -m crosscheck.cli ingest samples/delhivery-annual-report-fy25-excerpt.pdf`, and the
overlap makes incremental ingestion visible rather than merely claimed. Measured against a
copy of the database above:

- 19 blocks, 334 grounded facts from 377 proposed (89%)
- identified as "Annual Report 2024-25", Delhivery Limited — inferred, not configured
- **358 new relations to the already-ingested corpus, every one decided by rule**:
  314 reconciled-by-context, 41 corroborations, 3 derived

The clearest corroboration: `total income (INR)` = ₹85,942.34 million for FY2023-24 appears
in the FY24 report *and* in the FY25 report's comparative column, matched on the claim key
rather than on text. The clearest reconciliation comes from inside the new file alone --
`revenue from operations` for FY2024-25 is ₹82,524.47 million standalone and ₹89,319.01
million consolidated, separated by scope rather than called a contradiction.

Extraction needs a key. Ingestion, layout reconstruction and block-building do not, so
`ingest` alone works on this file with no credentials at all.

## What this does not include

Uploading a *new* PDF still needs an API key (`.env`, see `.env.example`) — this sample only
lets you inspect the output of a run that already happened. `docs/four-cases.md` cites exact
fact and relation IDs from this same database, so its examples are reproducible directly
against this file.
