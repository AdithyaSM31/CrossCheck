"""The grounding gate.

A model saying "page 12 states X" is not evidence. Every proposed fact has to survive two
checks against the exact text the model was shown:

1. **The quote must exist.** Verbatim first; failing that, a fuzzy alignment above a high
   threshold, which forgives whitespace and punctuation drift but not invention.
2. **The value must be inside the matched span.** This is the cheap check that catches the
   most dangerous failure mode — a real quote paired with a number lifted from a neighbouring
   table row. The quote verifies, the fact is wrong, and nothing downstream would notice.

Anything that fails is rejected into a review queue rather than dropped, because the failures
are evidence about the extractor and belong in the product, not in a log file.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

FUZZY_THRESHOLD = 88.0

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = {
    ord("‘"): "'", ord("’"): "'", ord("‚"): "'",
    ord("“"): '"', ord("”"): '"', ord("„"): '"',
}


def normalise(text: str) -> str:
    """Fold away the differences that are never meaningful: unicode form, dash and quote
    variants, ligatures, and whitespace runs."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("ﬁ", "fi").replace("ﬂ", "fl")
    text = text.translate(_DASHES).translate(_QUOTES)
    return " ".join(text.split()).lower()


@dataclass
class Grounding:
    status: str  # "verbatim" | "fuzzy" | "failed"
    score: float
    start: int
    end: int
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("verbatim", "fuzzy")


def _best_window(needle: str, hay: str) -> tuple[int, int, float]:
    """Best matching window for ``needle`` in ``hay``, by edit distance over a sliding
    window anchored on the strongest partial alignment."""
    if not needle or not hay:
        return -1, -1, 0.0
    n = len(needle)
    best = (-1, -1, 0.0)
    step = max(1, n // 8)
    for start in range(0, max(1, len(hay) - n + 1), step):
        window = hay[start : start + n]
        score = Levenshtein.normalized_similarity(needle, window) * 100
        if score > best[2]:
            best = (start, start + n, score)
    # Refine around the best coarse hit.
    lo = max(0, best[0] - step)
    for start in range(lo, min(len(hay) - n + 1, best[0] + step + 1)):
        window = hay[start : start + n]
        score = Levenshtein.normalized_similarity(needle, window) * 100
        if score > best[2]:
            best = (start, start + n, score)
    return best


def verify_quote(quote: str, source: str) -> Grounding:
    """Locate a quote in the text the model was actually shown."""
    if not quote or not quote.strip():
        return Grounding("failed", 0.0, -1, -1, "empty quote")

    nq, ns = normalise(quote), normalise(source)

    idx = ns.find(nq)
    if idx != -1:
        return Grounding("verbatim", 100.0, idx, idx + len(nq))

    # Short quotes get the exact test but never the fuzzy one. Fuzzy matching on a handful
    # of characters finds a match almost anywhere, so it would wave through exactly the
    # invented evidence this gate exists to stop. Rejecting them outright, as an earlier
    # version did, was worse: it discarded a sixth of all correctly grounded facts, whose
    # quotes were short simply because the source was a table cell or a slide caption.
    if len(nq) < 12:
        return Grounding("failed", 0.0, -1, -1, "short quote not found verbatim")

    # Cheap reject before the expensive window scan.
    if fuzz.partial_ratio(nq, ns) < FUZZY_THRESHOLD:
        return Grounding(
            "failed", fuzz.partial_ratio(nq, ns), -1, -1,
            "quote not found in the source text",
        )

    start, end, score = _best_window(nq, ns)
    if score >= FUZZY_THRESHOLD:
        return Grounding("fuzzy", score, start, end)
    return Grounding("failed", score, -1, -1, "quote not found in the source text")


_DIGITS = re.compile(r"\d")


def value_in_span(value: str, span: str) -> tuple[bool, str]:
    """Is the reported value actually inside the matched evidence?

    Catches a genuine quote carrying a number that came from somewhere else — the failure
    that looks perfectly grounded from the outside.
    """
    nv, nspan = normalise(value), normalise(span)
    if not nv:
        return False, "empty value"
    if nv in nspan:
        return True, ""

    # Numbers survive formatting differences that the raw string does not: a cell rendered
    # "2023/24=9.2" holds the value "9.2 per cent".
    v_digits = "".join(_DIGITS.findall(nv))
    if v_digits:
        s_digits = "".join(_DIGITS.findall(nspan))
        if v_digits in s_digits:
            return True, ""
        return False, f"value digits '{v_digits}' not present in the quoted evidence"

    # Non-numeric values (a status, a name) are matched on words.
    words = [w for w in re.findall(r"[a-z]{3,}", nv)]
    if words and all(w in nspan for w in words):
        return True, ""
    return False, "value not present in the quoted evidence"


def ground(quote: str, value: str, source: str) -> tuple[Grounding, str]:
    """Run both gates. Returns the grounding and a rejection reason (empty if it passed)."""
    g = verify_quote(quote, source)
    if not g.ok:
        return g, g.reason

    span = normalise(source)[g.start : g.end]
    ok, why = value_in_span(value, span)
    if not ok:
        return Grounding("failed", g.score, g.start, g.end, why), why
    return g, ""
