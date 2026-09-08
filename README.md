# CrossCheck — a fact knowledge layer

Extracts grounded facts from PDFs, links every fact to verified evidence in its source
document, and explains when facts corroborate, contradict, or only *appear* to conflict.

> **Status: in progress.** Ingest and the deterministic normalisation core are complete
> (milestones M0–M1). Extraction, grounding, reconciliation, the API and the UI are next.
> See [PLAN.md](PLAN.md) for the full design and milestones.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then add an API key; Groq's free tier works
```

## Run what exists today

```bash
python -m crosscheck.cli ingest "starter-datasets/delhivery/*.pdf"
python -m crosscheck.cli docs
python -m crosscheck.cli blocks 1 --page 4 --full
python -m pytest -q
```

## Approach

The system is built on one object, the **claim key** —
`(subject, attribute, period, scope, basis, unit_family)`:

| Claim keys | Values | Relation |
|---|---|---|
| equal | agree within tolerance | **corroborates** |
| equal | disagree | **contradicts** |
| differ in one component | — | **reconciled by context**, and the differing component *is* the explanation |

Two numbers are only comparable once you agree what claim they are making, so the qualifiers
that make them comparable are kept out of the attribute name and modelled explicitly.

The second commitment is that **grounding is verified, not asserted**: a quote must be found
in the source text and the value must appear inside the matched span, or the fact is rejected
into a review queue rather than entering the knowledge layer.

Full rationale, architecture and trade-offs: **[PLAN.md](PLAN.md)**.
