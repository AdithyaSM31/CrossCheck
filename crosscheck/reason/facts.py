"""A fact as the reconciler sees it: the claim, its normalised value, and its provenance."""

from __future__ import annotations

from dataclasses import dataclass

from ..normalize.periods import Period, parse_period
from ..normalize.values import ParsedValue, ValueKind


@dataclass
class FactView:
    id: int
    doc_id: int
    subject: str
    attribute: str
    attribute_raw: str
    value_raw: str
    value_num: float | None
    unit_family: str
    sig_figs: int
    period_label: str | None
    scope: str | None
    basis: str | None
    claim_key: str
    evidence_quote: str
    evidence_page: int
    confidence: float
    doc_title: str
    publisher: str
    published_on: str | None
    grounding: str = "verbatim"

    @property
    def grounding_rank(self) -> int:
        return {"verbatim": 2, "fuzzy": 1}.get(self.grounding, 0)

    @property
    def period(self) -> Period | None:
        return parse_period(self.period_label) if self.period_label else None

    @property
    def parsed(self) -> ParsedValue:
        return ParsedValue(
            kind=ValueKind.NUMBER,
            number=self.value_num,
            unit=None,
            unit_family=self.unit_family or "number",
            raw=self.value_raw,
            sig_figs=self.sig_figs or 0,
        )

    def cite(self) -> str:
        return f"{self.publisher or self.doc_title} p{self.evidence_page + 1}"

    def describe(self) -> str:
        bits = [b for b in (self.period_label, self.scope, self.basis) if b]
        qual = f" ({'; '.join(bits)})" if bits else ""
        return f"{self.subject} — {self.attribute} = {self.value_raw}{qual}"


SELECT = """
SELECT f.id, f.doc_id, f.subject, f.attribute_raw, f.value_raw, f.value_num,
       f.unit_family, f.sig_figs, f.period_label, f.scope, f.basis, f.claim_key,
       f.evidence_quote, f.evidence_page, f.confidence, f.grounding,
       COALESCE(a.canon_name, f.attribute_raw) attribute,
       COALESCE(d.title, d.filename) doc_title,
       COALESCE(d.publisher, '') publisher, d.published_on
  FROM facts f
  JOIN documents d ON d.id = f.doc_id
  LEFT JOIN attributes a ON a.id = f.attribute_id
"""


def dedupe(facts: list[FactView]) -> list[FactView]:
    """Collapse restatements of the same claim within one document.

    A figure is routinely extracted several times from one document — the headline slide,
    the metrics table, the commentary. Each copy is a real fact, but for reconciliation they
    are one claim, and leaving them in multiplies every relation they take part in: the same
    corroboration came back three times purely because two facts each had duplicates.

    The surviving copy is the best-evidenced one, so the relation cites the clearest quote.
    """
    best: dict[tuple, FactView] = {}
    for f in facts:
        key = (f.doc_id, f.claim_key, (f.value_raw or "").strip().lower())
        current = best.get(key)
        if current is None or _evidence_rank(f) > _evidence_rank(current):
            best[key] = f
    return sorted(best.values(), key=lambda f: f.id)


def _evidence_rank(f: FactView) -> tuple:
    """Prefer a verbatim quote, then a longer one, then higher confidence.

    Quote length is a real signal: a fact evidenced by "12.7%" is grounded but tells a
    reader nothing, while the same fact evidenced by the sentence around it is checkable.
    """
    return (
        f.grounding_rank,
        min(len(f.evidence_quote or ""), 400),
        f.confidence,
    )


def load_facts(conn, where: str = "", params: tuple = ()) -> list[FactView]:
    rows = conn.execute(SELECT + (f" WHERE {where}" if where else ""), params).fetchall()
    return [
        FactView(
            id=r["id"], doc_id=r["doc_id"], subject=r["subject"],
            attribute=r["attribute"], attribute_raw=r["attribute_raw"],
            value_raw=r["value_raw"], value_num=r["value_num"],
            unit_family=r["unit_family"] or "", sig_figs=r["sig_figs"] or 0,
            period_label=r["period_label"], scope=r["scope"], basis=r["basis"],
            claim_key=r["claim_key"] or "", evidence_quote=r["evidence_quote"],
            evidence_page=r["evidence_page"], confidence=r["confidence"] or 0.0,
            doc_title=r["doc_title"], publisher=r["publisher"],
            published_on=r["published_on"], grounding=r["grounding"] or "verbatim",
        )
        for r in rows
    ]
