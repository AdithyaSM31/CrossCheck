"""Corroboration between facts expressed differently.

Two sources can agree without stating the same number. An earnings deck reports EBITDA of
Rs.127 Cr, revenue from services of Rs.8,142 Cr, and an EBITDA margin of 1.6%. No pair of
those is the same claim, so claim-key matching says nothing — yet the margin is exactly what
the other two imply, and that is a real corroboration a reader would care about.

No formula is hard-coded. The checker looks for arithmetic relationships that *hold*, among
facts that already share a subject and a period, and requires the attribute names to overlap
before believing a coincidence. Both patterns generalise to documents this system has never
seen:

* **ratio** — some percentage equals one amount divided by another
* **growth** — some percentage equals the change between the same measure in two periods

The name-overlap requirement is what keeps this from being numerology. Among a few dozen
figures sharing a period, some pair will divide into some percentage by chance; requiring
"ebitda margin" to share a word with "ebitda" makes the coincidence mean something.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..normalize.periods import relation, PeriodRelation
from .facts import FactView
from .keys import normalise_phrase

TOLERANCE = 0.06  # relative; sources round their own ratios
_STOPWORDS = {"total", "net", "gross", "value", "amount", "rate", "growth", "margin",
              "share", "ratio", "yoy", "change", "increase", "percentage", "per", "cent"}


@dataclass
class Derivation:
    kind: str  # "ratio" | "growth"
    target: FactView  # the stated percentage
    parts: list[FactView]  # the facts that imply it
    computed: float
    stated: float
    explanation: str = ""

    @property
    def error(self) -> float:
        denom = max(abs(self.stated), 1e-9)
        return abs(self.computed - self.stated) / denom


def _content_words(text: str) -> set[str]:
    return {w for w in normalise_phrase(text).split() if w not in _STOPWORDS and len(w) > 2}


def _related(a: str, b: str) -> bool:
    """Do two attribute names share a subject word? Guards against numeric coincidence."""
    wa, wb = _content_words(a), _content_words(b)
    return bool(wa & wb)


def _is_percent(f: FactView) -> bool:
    return f.unit_family == "percent" and f.value_num is not None and _usable(f)


MIN_EVIDENCE_CHARS = 18


def _usable(f: FactView) -> bool:
    """Facts whose evidence is a bare fragment are excluded from arithmetic.

    A garbled table row produces values like "173" quoted as "(Rs.: FY24=8%; Services
    revenue=173". Those divide into some percentage as readily as real ones, and the result
    is a confident-looking corroboration built on a parsing failure.
    """
    return len(f.evidence_quote or "") >= MIN_EVIDENCE_CHARS


def _is_level(f: FactView) -> bool:
    return (
        _usable(f)
        and f.value_num not in (None, 0)
        and (f.unit_family.startswith("currency:") or f.unit_family in ("number",)
             or f.unit_family.startswith("quantity:"))
    )


def _period_key(f: FactView) -> str:
    p = f.period
    return p.key() if p else ""


def find_ratios(facts: list[FactView]) -> list[Derivation]:
    """Percentages that equal one amount divided by another, in the same subject and period."""
    out: list[Derivation] = []
    groups: dict[tuple[str, str], list[FactView]] = {}
    for f in facts:
        if not _period_key(f):
            continue
        groups.setdefault((normalise_phrase(f.subject), _period_key(f)), []).append(f)

    for group in groups.values():
        if len(group) < 3:
            continue
        percents = [f for f in group if _is_percent(f)]
        levels = [f for f in group if _is_level(f)]
        if not percents or len(levels) < 2:
            continue

        for target in percents:
            best: Derivation | None = None
            for num in levels:
                if not _related(target.attribute, num.attribute):
                    continue
                for den in levels:
                    if den.id == num.id or den.unit_family != num.unit_family:
                        continue
                    if den.value_num in (None, 0):
                        continue
                    computed = num.value_num / den.value_num * 100.0
                    d = Derivation("ratio", target, [num, den], computed,
                                   target.value_num or 0.0)
                    if d.error <= TOLERANCE and (best is None or d.error < best.error):
                        best = d
            if best is not None:
                best.explanation = (
                    f"{best.parts[0].value_raw} ÷ {best.parts[1].value_raw} "
                    f"= {best.computed:.2f}%, matching the stated {target.value_raw}"
                )
                out.append(best)
    return out


def find_growth(facts: list[FactView]) -> list[Derivation]:
    """Percentages that equal the change in the same measure between two periods."""
    out: list[Derivation] = []
    by_subject: dict[str, list[FactView]] = {}
    for f in facts:
        by_subject.setdefault(normalise_phrase(f.subject), []).append(f)

    for group in by_subject.values():
        percents = [f for f in group if _is_percent(f) and f.period]
        levels = [f for f in group if _is_level(f) and f.period]
        if not percents or len(levels) < 2:
            continue

        for target in percents:
            best: Derivation | None = None
            for now in levels:
                if not _related(target.attribute, now.attribute):
                    continue
                if relation(target.period, now.period) is not PeriodRelation.EQUAL:
                    continue
                for prev in levels:
                    if prev.id == now.id or prev.unit_family != now.unit_family:
                        continue
                    if not _related(now.attribute, prev.attribute):
                        continue
                    # The earlier period must sit immediately before the later one.
                    if prev.period.end >= now.period.start or prev.value_num in (None, 0):
                        continue
                    if (now.period.start - prev.period.end).days > 40:
                        continue
                    computed = (now.value_num - prev.value_num) / abs(prev.value_num) * 100.0
                    d = Derivation("growth", target, [prev, now], computed,
                                   target.value_num or 0.0)
                    if d.error <= TOLERANCE and (best is None or d.error < best.error):
                        best = d
            if best is not None:
                prev, now = best.parts
                best.explanation = (
                    f"{prev.value_raw} ({prev.period_label}) to {now.value_raw} "
                    f"({now.period_label}) is {best.computed:+.1f}%, matching the stated "
                    f"{target.value_raw}"
                )
                out.append(best)
    return out


def find_all(facts: list[FactView]) -> list[Derivation]:
    return find_ratios(facts) + find_growth(facts)
