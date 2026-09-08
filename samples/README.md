# Sample output

`crosscheck.sample.db` is the real, fully-populated database from the run that produced this
submission: all six starter documents ingested and extracted (5,835 grounded facts), the
attribute vocabulary consolidated (~3,300 canonical attributes), and reconciliation run
against the whole corpus (~650 relations — corroborations, contradictions, context-explained
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

## What this does not include

Uploading a *new* PDF still needs an API key (`.env`, see `.env.example`) — this sample only
lets you inspect the output of a run that already happened. `docs/four-cases.md` cites exact
fact and relation IDs from this same database, so its examples are reproducible directly
against this file.
