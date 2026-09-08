"""Deterministic adjudication.

Rules decide the *label*; the model is only asked for the cases rules genuinely cannot
settle, and for the wording of an explanation. Every relation records which decided it, so
the reasoning is auditable rather than a black box, and a rule disagreeing with the model is
itself a signal worth surfacing.

The logic is small because the claim key already did the hard part:

    keys identical, values agree      -> CORROBORATES
    keys identical, values disagree   -> a real disagreement; ask the model whether it is a
                                         contradiction or a later vintage revising an earlier
    keys differ in one component,
      and the values disagree         -> RECONCILED_BY_CONTEXT, and that component is the
                                         explanation

That last line is the whole of case 3. Note the second condition: if two facts carry
different periods and different values, there is nothing to explain — that is simply what
different periods look like. An explanation is only worth producing where a reader would
otherwise see a conflict.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..normalize.periods import PeriodRelation, relation
from ..normalize.values import comparable, values_agree
from .facts import FactView
from .keys import differing_components

CORROBORATES = "CORROBORATES"
CONTRADICTS = "CONTRADICTS"
RECONCILED = "RECONCILED_BY_CONTEXT"
SUPERSEDES = "SUPERSEDES"
DERIVED = "DERIVED_CONSISTENT"


@dataclass
class Verdict:
    label: str | None
    discriminator: str = ""
    confidence: float = 0.0
    rule: str = ""
    needs_llm: bool = False
    detail: str = ""

    @property
    def actionable(self) -> bool:
        return bool(self.label) or self.needs_llm


def _same_document(a: FactView, b: FactView) -> bool:
    return a.doc_id == b.doc_id


def _explain_period(a: FactView, b: FactView) -> str:
    pa, pb = a.period, b.period
    if pa is None or pb is None:
        return "period (one source does not state one)"
    rel = relation(pa, pb)
    if rel in (PeriodRelation.CONTAINS, PeriodRelation.CONTAINED_BY):
        return "period (one covers part of the other)"
    if rel is PeriodRelation.OVERLAPS:
        return "period (overlapping but not identical)"
    return "period"


def classify(a: FactView, b: FactView) -> Verdict:
    """Adjudicate a candidate pair as far as rules allow."""
    if a.id == b.id:
        return Verdict(None)

    # Non-numeric facts (a directorship, an address) cannot be compared arithmetically.
    # They are still worth adjudicating, but only the model can read them.
    numeric = a.value_num is not None and b.value_num is not None
    diffs = differing_components(a.claim_key, b.claim_key)

    if not diffs:
        if not numeric:
            same_text = a.value_raw.strip().lower() == b.value_raw.strip().lower()
            if same_text:
                return Verdict(
                    CORROBORATES, "", 0.9, "identical claim, identical statement"
                )
            return Verdict(
                None, "", 0.0, "identical claim, differing statements", needs_llm=True,
                detail="Same claim key, but the two statements differ in wording or content.",
            )

        if not comparable(a.parsed, b.parsed):
            return Verdict(None)

        agree, why = values_agree(a.parsed, b.parsed)
        if agree:
            return Verdict(CORROBORATES, "", 0.95, f"identical claim, values {why}")

        # A real disagreement on identical claims. Whether it is a contradiction or a
        # revision depends on the documents' vintages and on what each source says about
        # firmness — an estimate, a projection, a revision. That is a reading task.
        return Verdict(
            None, "", 0.0, "identical claim, values disagree", needs_llm=True,
            detail=f"Same claim key; values differ ({why}).",
        )

    # Different claims. Only interesting when a reader would see a conflict, which means
    # the values have to actually disagree.
    if numeric and comparable(a.parsed, b.parsed):
        agree, _ = values_agree(a.parsed, b.parsed)
        if agree:
            # Same number under different qualifiers: the qualifier did not change the
            # answer, so the sources still corroborate one another.
            if diffs == ["period"]:
                return Verdict(None)
            return Verdict(
                CORROBORATES, ", ".join(diffs), 0.75,
                "values agree despite differing " + ", ".join(diffs),
            )

    if len(diffs) == 1:
        dim = diffs[0]
        # A reconciliation is only worth reporting if both sides are stated with some
        # confidence. The bulk of the noise came from duplicate extractions of the same
        # figure rather than from weak facts, and deduplicating the fact set upstream
        # removes it at the source -- a page-proximity filter would also have thrown away
        # the genuine case where a deck states the year on one page and the quarter on
        # the next.
        if min(a.confidence, b.confidence) < 0.5:
            return Verdict(None)
        if dim == "period":
            return Verdict(
                RECONCILED, _explain_period(a, b), 0.9, "single differing qualifier: period"
            )
        if dim in ("scope", "basis", "unit"):
            return Verdict(
                RECONCILED, dim, 0.85, f"single differing qualifier: {dim}"
            )
        # Subject or attribute differing means the loose key matched but the claims are
        # not the same measure; nothing to say.
        return Verdict(None)

    # More than one qualifier differs. Too weak to assert on rules alone, but worth the
    # model's time when both sides are confident and the values conflict.
    if numeric and comparable(a.parsed, b.parsed) and not _same_document(a, b):
        agree, _ = values_agree(a.parsed, b.parsed)
        if not agree and min(a.confidence, b.confidence) >= 0.6:
            return Verdict(
                None, ", ".join(diffs), 0.0, "several differing qualifiers",
                needs_llm=True,
                detail=f"Claims differ in {', '.join(diffs)} and the values conflict.",
            )

    return Verdict(None)
