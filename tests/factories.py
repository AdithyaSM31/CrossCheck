"""Shared builder for FactView objects in tests."""

from crosscheck.normalize.periods import parse_period
from crosscheck.normalize.values import parse_value
from crosscheck.reason.facts import FactView
from crosscheck.reason.keys import claim_key

_next_id = iter(range(1, 100_000))


def mk(
    attribute,
    value,
    *,
    subject="Delhivery Limited",
    period=None,
    scope=None,
    basis=None,
    doc_id=1,
    publisher="Delhivery",
    published_on="2024-05-17",
    confidence=0.9,
):
    parsed = parse_value(value)
    p = parse_period(period) if period else None
    return FactView(
        id=next(_next_id),
        doc_id=doc_id,
        subject=subject,
        attribute=attribute,
        attribute_raw=attribute,
        value_raw=value,
        value_num=parsed.number,
        unit_family=parsed.unit_family,
        sig_figs=parsed.sig_figs,
        period_label=p.label if p else None,
        scope=scope,
        basis=basis,
        claim_key=claim_key(
            subject=subject, attribute=attribute, period=p,
            scope=scope, basis=basis, unit_family=parsed.unit_family,
        ),
        evidence_quote=f"... {value} ...",
        evidence_page=1,
        confidence=confidence,
        doc_title="doc",
        publisher=publisher,
        published_on=published_on,
    )
