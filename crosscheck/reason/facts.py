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
       f.evidence_quote, f.evidence_page, f.confidence,
       COALESCE(a.canon_name, f.attribute_raw) attribute,
       COALESCE(d.title, d.filename) doc_title,
       COALESCE(d.publisher, '') publisher, d.published_on
  FROM facts f
  JOIN documents d ON d.id = f.doc_id
  LEFT JOIN attributes a ON a.id = f.attribute_id
"""


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
            published_on=r["published_on"],
        )
        for r in rows
    ]
