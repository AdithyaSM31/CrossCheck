"""The extraction prompt.

Two instructions here carry most of the system's weight.

**Qualifiers stay out of the attribute name.** An attribute of "FY24 consolidated revenue in
crore" is unusable: it can only ever match itself. Forcing period, scope, basis and unit into
separate fields is what makes two facts from different documents comparable at all, and it is
what lets the reconciler say *which* dimension explains an apparent conflict.

**Evidence must be copied, not composed.** The quote is checked character-for-character
against the source text afterwards, and the value must be found inside it. A model that
paraphrases loses its fact. That is deliberate: an unverifiable claim is worse than a missing
one, because it looks exactly like a real one.
"""

from __future__ import annotations

SYSTEM = """\
You extract verifiable facts from documents. You never infer, never calculate, and never use \
outside knowledge. If the text does not state it, it is not a fact.

Return a JSON object: {"facts": [...]}. Each fact has these keys:

- "subject": the entity the fact is about, named as the document names it
  (e.g. "Delhivery Limited", "India", "Sunil Kumar Bansal").
- "attribute": a short lowercase noun phrase naming WHAT is measured or stated
  (e.g. "revenue from services", "real GDP growth", "pin-code reach", "directorship status").
  CRITICAL: never put the time period, scope, basis, unit or currency in the attribute.
    good  -> "revenue from services"
    bad   -> "FY24 consolidated revenue from services in crore"
  Those belong in the fields below. An attribute carrying its qualifiers can never be
  compared with the same attribute from another document.
- "value": exactly as written in the text, keeping currency, sign, brackets, scale and unit
  (e.g. "Rs.8,142 Cr", "(6.3%)", "6.5 per cent", "18,074", "1.4 Mn Tons", "resigned").
- "value_kind": one of money | percent | count | ratio | date | text | entity.
- "period": the period exactly as written ("FY24", "2024-25", "Q4 FY24", "FY2024/25",
  "as of March 31, 2024"), or null if the text does not state one. NEVER guess a period.
- "scope": "consolidated", "standalone", a segment or subsidiary name, or null.
- "basis": any qualifier that changes what the number counts or how firm it is
  ("excluding revenue from traded goods", "advance estimate", "revised estimate",
  "projection", "annualised", "seasonally adjusted"), or null.
- "geography": the place the fact applies to, or null.
- "evidence_quote": a span copied VERBATIM from the supplied text that contains the value.
- "confidence": 0.0-1.0, your confidence that this fact is correctly read.

Rules:
1. Copy "evidence_quote" character-for-character from the text. Do not paraphrase, correct,
   reword, translate or join separate lines. It is checked against the source; a quote that
   does not match exactly causes the fact to be discarded.
2. The "value" must appear inside its own "evidence_quote".
3. For a table row, quote the whole row line, including its label and every cell.
4. A table row reads "label: header=value; header=value". Each cell is a SEPARATE fact whose
   period comes from that cell's own header. Never carry one cell's value to another header.
5. Lines in [square brackets] are section banners or footnotes. They supply unit, currency
   and basis for the rows they govern - use them to fill "basis" and "value_kind", but quote
   the row itself, not the banner.
6. Use null for anything the text does not state. Guessing a qualifier is worse than
   omitting it, because it creates a false disagreement with another document.
7. Extract non-numeric facts too: appointments, resignations, registered addresses, auditors,
   listing dates, ratings, ownership.
8. Do not extract page numbers, contents entries, cross-references, section numbers, or
   legal boilerplate.
9. If the text states nothing factual, return {"facts": []}. An empty answer is a good
   answer; an invented one is not.
"""

USER_TEMPLATE = """\
Document: {title}
Publisher: {publisher}
{provenance}
Page: {page}

--- TEXT ---
{text}
--- END TEXT ---

Extract every fact stated in the text above, following the rules exactly.
"""


def build_user_prompt(
    *, text: str, title: str, publisher: str, page: str, provenance: str = ""
) -> str:
    return USER_TEMPLATE.format(
        text=text,
        title=title or "(unknown)",
        publisher=publisher or "(unknown)",
        page=page,
        provenance=provenance or "",
    )


# ------------------------------------------------------------------ document metadata
META_SYSTEM = """\
You identify a document from its opening pages. Return a JSON object with these keys:

- "title": the document's title as printed.
- "publisher": the organisation that issued it (e.g. "Delhivery Limited",
  "Reserve Bank of India", "International Monetary Fund", "Government of India").
- "doc_type": one of prospectus | annual_report | earnings_presentation | staff_report |
  survey | filing | other.
- "published_on": the publication date as an ISO date (YYYY-MM-DD), or null.
- "as_of": the date the document's data is current to (ISO), or null.
- "primary_subject": the main entity the document is about.

Use null rather than guessing. The publication date matters: it is what later distinguishes
a later report revising an earlier estimate from two sources genuinely contradicting.
"""
