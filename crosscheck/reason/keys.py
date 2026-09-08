"""The claim key.

Two numbers are only comparable once you agree what claim they are making. The claim key is
that agreement, made explicit:

    (subject, attribute, period, scope, basis, unit_family)

Equal keys mean the facts are talking about exactly the same thing, so their values can be
compared directly — agreement is corroboration, disagreement is contradiction. Unequal keys
mean they are not, and the component that differs *is* the explanation for an apparent
conflict: the same revenue attribute over FY24 and Q4 FY24 differs by period, not by fact.

Keys are built from the interval a period denotes, never its label, so the Economic Survey's
"FY25", the RBI's "2024-25" and the IMF's "FY2024/25" all land on the same key.
"""

from __future__ import annotations

import re

from ..normalize.periods import Period

_NOISE = re.compile(r"[^a-z0-9 ]+")
_STOP = {"the", "a", "an", "of", "for", "in", "on", "at", "to", "and", "s", "total"}


def normalise_phrase(text: str | None) -> str:
    """Lowercase, strip punctuation and filler, and sort nothing — word order carries
    meaning ("revenue growth" is not "growth revenue")."""
    if not text:
        return ""
    t = _NOISE.sub(" ", str(text).lower())
    words = [w for w in t.split() if w not in _STOP]
    return " ".join(words)


def subject_key(subject: str | None) -> str:
    """A coarse subject identity. Entity resolution proper refines this later; for now,
    'Delhivery Limited' and 'Delhivery Ltd.' should already collapse together."""
    s = normalise_phrase(subject)
    s = re.sub(r"\b(limited|ltd|inc|plc|corporation|corp|company|co)\b", "", s)
    return " ".join(s.split())


def claim_key(
    *,
    subject: str | None,
    attribute: str | None,
    period: Period | None,
    scope: str | None = None,
    basis: str | None = None,
    unit_family: str | None = None,
) -> str:
    """The join key. Every component is normalised; the period contributes its interval."""
    return "|".join(
        [
            subject_key(subject),
            normalise_phrase(attribute),
            period.key() if period else "",
            normalise_phrase(scope),
            normalise_phrase(basis),
            unit_family or "",
        ]
    )


def loose_key(subject: str | None, attribute: str | None) -> str:
    """Subject and attribute only.

    This is the *candidate* key: facts sharing it are about the same measure and are worth
    comparing, whether or not their qualifiers agree. Facts whose loose keys match but whose
    full claim keys differ are exactly the population where an apparent contradiction gets
    explained by context.
    """
    return f"{subject_key(subject)}|{normalise_phrase(attribute)}"


def differing_components(a: str, b: str) -> list[str]:
    """Which parts of two claim keys disagree. This is the discriminator the reconciler
    reports: "these differ by period", "these differ by unit"."""
    names = ["subject", "attribute", "period", "scope", "basis", "unit"]
    return [
        name
        for name, x, y in zip(names, a.split("|"), b.split("|"))
        if x != y
    ]
