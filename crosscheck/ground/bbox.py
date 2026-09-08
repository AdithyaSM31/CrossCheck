"""Locating verified evidence on the page.

Grounding proves a quote exists in the text the model was shown. This turns that into
coordinates, so the evidence can be shown circled on the page a reader would cite.

Two strategies, because the two kinds of block differ:

* **Prose** — the quote is a contiguous run of words on the page, so match the token
  sequence directly.
* **Tables** — the quote is a row we *synthesised* ("Real GDP (at market prices):
  2023/24=9.2"). Those characters never appear on the page in that order, so a sequence
  match cannot work. Instead the row label is located and the value is found within that
  row's band, which highlights the individual cell — more useful than boxing the whole row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..ingest.layout import Word

BBox = tuple[float, float, float, float]


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _merge_by_line(boxes: list[BBox], tol: float = 4.0) -> list[BBox]:
    """Union boxes that sit on the same visual line, so a highlight is one bar per line
    rather than a box per word."""
    if not boxes:
        return []
    lines: list[list[BBox]] = []
    for b in sorted(boxes, key=lambda b: (b[1], b[0])):
        for ln in lines:
            if abs(ln[0][1] - b[1]) <= tol:
                ln.append(b)
                break
        else:
            lines.append([b])
    return [
        (
            min(x[0] for x in ln), min(x[1] for x in ln),
            max(x[2] for x in ln), max(x[3] for x in ln),
        )
        for ln in lines
    ]


def _inside(box: BBox, within: BBox | None, pad: float = 6.0) -> bool:
    if within is None:
        return True
    return (
        box[2] >= within[0] - pad
        and box[0] <= within[2] + pad
        and box[3] >= within[1] - pad
        and box[1] <= within[3] + pad
    )


@dataclass
class Located:
    rects: list[BBox]
    how: str  # "quote" | "value" | "block" | "none"


def locate_sequence(quote: str, words: list[Word], within: BBox | None) -> list[BBox]:
    """Best contiguous run of page words matching the quote's tokens."""
    want = _tokens(quote)
    if len(want) < 3:
        return []
    candidates = [w for w in words if _inside((w.x0, w.y0, w.x1, w.y1), within)]
    have = [_tokens(w.text) for w in candidates]
    flat: list[tuple[str, int]] = [
        (tok, i) for i, toks in enumerate(have) for tok in toks
    ]
    if not flat:
        return []

    seq = [t for t, _ in flat]
    best_score, best_span = 0.0, None
    n = len(want)
    for start in range(max(1, len(seq) - n + 1)):
        window = seq[start : start + n]
        hits = sum(1 for a, b in zip(window, want) if a == b)
        score = hits / n
        if score > best_score:
            best_score = score
            best_span = (flat[start][1], flat[min(start + n, len(flat)) - 1][1])
        if score == 1.0:
            break

    if best_score < 0.75 or best_span is None:
        return []
    lo, hi = best_span
    return _merge_by_line(
        [(w.x0, w.y0, w.x1, w.y1) for w in candidates[lo : hi + 1]]
    )


def locate_value(value: str, words: list[Word], within: BBox | None) -> list[BBox]:
    """Find the value itself — the fallback for synthesised table rows.

    Matching on digits rather than the raw string, because the page writes "9.2" where the
    fact may carry "9.2 per cent", and "18,074" where the fact carries "18074".
    """
    digits = "".join(re.findall(r"\d", value))
    if not digits:
        toks = set(_tokens(value))
        hits = [
            (w.x0, w.y0, w.x1, w.y1)
            for w in words
            if toks & set(_tokens(w.text)) and _inside((w.x0, w.y0, w.x1, w.y1), within)
        ]
        return _merge_by_line(hits[:6])

    hits = []
    for w in words:
        box = (w.x0, w.y0, w.x1, w.y1)
        if not _inside(box, within):
            continue
        if "".join(re.findall(r"\d", w.text)) == digits:
            hits.append(box)
    return _merge_by_line(hits[:4])


def locate(
    *, quote: str, value: str, words: list[Word], block_bbox: BBox | None
) -> Located:
    """Best available rectangles for a fact's evidence, with how they were found."""
    rects = locate_sequence(quote, words, block_bbox)
    if rects:
        return Located(rects, "quote")

    rects = locate_value(value, words, block_bbox)
    if rects:
        return Located(rects, "value")

    # Falling back to the block keeps the evidence honest: it points at the right region
    # rather than pretending to a precision we do not have.
    return Located([block_bbox] if block_bbox else [], "block" if block_bbox else "none")
