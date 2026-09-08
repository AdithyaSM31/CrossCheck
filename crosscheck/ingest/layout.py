"""Geometric layout reconstruction.

PyMuPDF's own block grouping is unreliable on the documents in this corpus — the RBI annual
report yields one block per *line*, in a two-column layout, so naive blocking produces
wrapped fragments like "headline inflation" / "eased by 73 bps to 4.6 per cent". And
``find_tables`` shreds row labels on borderless financial tables ("Real GDP (a" |
"t market price" | ")").

So layout is reconstructed from word geometry instead:

* **Running headers and footers** are found by looking for short lines that repeat at the
  same height across many pages, and stripped. They also hand us the printed page label,
  which is what a reader would cite.
* **Tables are found by numeric alignment, not by ruling lines.** Financial tables are
  decimal-aligned, so the x-centres of numeric tokens cluster tightly even when no border
  exists and the label text is ragged. Columns are derived from those clusters, headers are
  attached by x-overlap, and each row is emitted as
  ``Real GDP (at market prices): 2021/22=9.7; 2022/23=7.6; ...`` — which keeps every value
  bound to its own column header instead of relying on positional alignment surviving.
* **Prose is re-flowed** per column, de-hyphenated, and split into paragraphs on vertical
  gaps and paragraph markers.

Everything keeps its word boxes, so evidence can still be located on the page.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

BBox = tuple[float, float, float, float]

# A data cell. '/' is excluded so period headers like "2021/22" are not mistaken for values.
_NUMERIC = re.compile(r"^[(\[]?[-−+]?\d[\d,\s]*(?:\.\d+)?[)\]]?%?$")
_PARA_MARKER = re.compile(
    r"^(?:[IVXLC]+\.\d+|\d+\.\d+(?:\.\d+)?|\(\w{1,3}\)|[•▪●○–—*]|\d+\.\s)"
)
_HEADERISH = re.compile(r"\d{4}|\bQ[1-4]\b|\bFY\b|\bH[12]\b|^\d{2}/\d{2}$", re.I)


def is_numeric_token(tok: str) -> bool:
    tok = tok.strip().rstrip(".,;")
    if not tok or "/" in tok:
        return False
    return bool(_NUMERIC.match(tok))


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float = 0.0
    bold: bool = False

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Line:
    words: list[Word]
    size: float = 0.0
    bold: bool = False

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def bbox(self) -> BBox:
        return (
            min(w.x0 for w in self.words),
            min(w.y0 for w in self.words),
            max(w.x1 for w in self.words),
            max(w.y1 for w in self.words),
        )

    @property
    def yc(self) -> float:
        return statistics.median([w.yc for w in self.words])

    @property
    def height(self) -> float:
        b = self.bbox
        return b[3] - b[1]


@dataclass
class Region:
    kind: str  # "table" | "prose"
    lines: list[Line]
    bbox: BBox
    rows: list[dict] = field(default_factory=list)  # tables only


# ------------------------------------------------------------------ lines from words
def build_lines(words: list[Word], tol_ratio: float = 0.6) -> list[Line]:
    """Group words into visual rows by vertical overlap.

    Row banding (rather than PyMuPDF's line index) is what lets a table row made of words
    from several different blocks come back as one row.
    """
    if not words:
        return []
    heights = [w.y1 - w.y0 for w in words if w.y1 > w.y0]
    tol = (statistics.median(heights) if heights else 8.0) * tol_ratio

    lines: list[Line] = []
    for w in sorted(words, key=lambda w: (w.yc, w.x0)):
        for ln in reversed(lines[-3:]):
            if abs(ln.yc - w.yc) <= tol:
                ln.words.append(w)
                break
        else:
            lines.append(Line([w]))
    for ln in lines:
        ln.words.sort(key=lambda w: w.x0)
        ln.size = statistics.median([w.size for w in ln.words] or [0.0])
        ln.bold = sum(w.bold for w in ln.words) > len(ln.words) / 2
    lines.sort(key=lambda ln: (ln.yc, ln.bbox[0]))
    return lines


# ------------------------------------------------------------ running headers/footers
def find_running_lines(pages_lines: list[list[Line]], page_height: float) -> set[str]:
    """Short lines that recur near the top or bottom of many pages are page furniture."""
    if len(pages_lines) < 4:
        return set()
    counts: Counter[str] = Counter()
    for lines in pages_lines:
        seen = set()
        for ln in lines:
            y = ln.yc
            if y > page_height * 0.10 and y < page_height * 0.90:
                continue
            t = re.sub(r"\d+", "#", ln.text.strip())
            if 3 <= len(t) <= 90:
                seen.add(t)
        counts.update(seen)
    threshold = max(3, int(len(pages_lines) * 0.35))
    return {t for t, n in counts.items() if n >= threshold}


def page_label(lines: list[Line], page_height: float) -> str | None:
    """The page number printed on the page, which is what a reader would cite."""
    for ln in lines:
        if ln.yc > page_height * 0.90 or ln.yc < page_height * 0.10:
            t = ln.text.strip()
            if re.fullmatch(r"[ivxlcIVXLC]{1,7}|\d{1,4}", t):
                return t
    return None


# ---------------------------------------------------------------- table detection
def _numeric_count(ln: Line) -> int:
    return sum(1 for w in ln.words if is_numeric_token(w.text))


def find_table_regions(lines: list[Line]) -> list[tuple[int, int]]:
    """Index ranges of lines that form a table, found by rows of aligned numbers."""
    tabular = [_numeric_count(ln) >= 2 for ln in lines]
    regions: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if not tabular[i]:
            i += 1
            continue
        j = i
        gap = 0
        while j + 1 < len(lines) and gap <= 1:
            j += 1
            gap = 0 if tabular[j] else gap + 1
        end = j - gap
        if end - i + 1 >= 3:  # three aligned rows before we call it a table
            start = i
            # Pull in the header rows sitting above the numeric body. Financial tables
            # stack several short rows there — a year row, then "Est. | Projections", then
            # a section caption — so keep climbing through short lines rather than
            # stopping at the first one that is not itself period-like. Without this the
            # year header is never reached and every value loses its period.
            while start > 0 and start - i > -5:
                prev = lines[start - 1]
                if (
                    _numeric_count(prev) >= 2
                    or _HEADERISH.search(prev.text)
                    or len(prev.words) <= 12
                ):
                    start -= 1
                else:
                    break
            regions.append((start, end))
        i = max(j + 1, i + 1)
    return regions


# "(1)", "(3)" trailing a row label are footnote markers, not data. Left in, they cluster
# into a phantom column sitting on top of the label text, which then eats the row label:
# "Pin-code reach(1)" collapses to a cell reading "Pin-code".
_FOOTNOTE_REF = re.compile(r"^\(\d\)$")


def _cluster_columns(lines: list[Line], tol: float) -> list[float]:
    """Column centres, taken from where numeric tokens line up."""
    centres = [
        w.xc
        for ln in lines
        for w in ln.words
        if is_numeric_token(w.text) and not _FOOTNOTE_REF.match(w.text.strip())
    ]
    if len(centres) < 3:
        return []
    centres.sort()
    groups: list[list[float]] = [[centres[0]]]
    for c in centres[1:]:
        if c - groups[-1][-1] <= tol:
            groups[-1].append(c)
        else:
            groups.append([c])
    # A real column has several values in it; stray in-sentence numbers do not.
    return [statistics.median(g) for g in groups if len(g) >= 2]


def reconstruct_table(
    lines: list[Line], page_width: float
) -> tuple[list[dict], str]:
    """Rows as ``{label, cells: {header: value}, bbox}``, plus the table's caption.

    Values are bound to headers by geometry, so a shredded row label or a merged header
    cell cannot silently shift a number into the wrong year.
    """
    tol = page_width * 0.02
    cols = _cluster_columns(lines, tol)
    if not cols:
        return [], ""
    left_edge = min(cols) - tol * 2

    def col_of(w: Word) -> int | None:
        best, dist = None, tol * 1.8
        for i, c in enumerate(cols):
            d = abs(w.xc - c)
            if d < dist:
                best, dist = i, d
        return best

    # --- header row -------------------------------------------------------------------
    # Score every candidate line above the numeric body and take the best one, rather than
    # filling slots first-come-first-served. A table's *title* also sits above the body and
    # contains a year range ("Table 1. ... 2021/22-2026/27"), so it wins any first-come
    # race and scatters its own words across the column headers. The discriminator is
    # geometry: a header row lives inside the column band, a title spans the page.
    def _slots_for(ln: Line) -> dict[int, list[str]]:
        inband = [w for w in ln.words if w.x0 >= left_edge]
        if not ln.words or len(inband) / len(ln.words) < 0.6:
            return {}
        slots: dict[int, list[str]] = {}
        for w in inband:
            ci = col_of(w)
            if ci is not None:
                slots.setdefault(ci, []).append(w.text)
        return slots

    header_idx, best_slots, best_score = -1, {}, 0
    for idx, ln in enumerate(lines[:8]):
        if _numeric_count(ln) >= 2 and not _HEADERISH.search(ln.text):
            break
        slots = _slots_for(ln)
        if len(slots) < 2:
            continue
        periodish = sum(1 for toks in slots.values() if _HEADERISH.search(" ".join(toks)))
        score = len(slots) + 2 * periodish
        if score > best_score:
            header_idx, best_slots, best_score = idx, slots, score

    headers: list[str] = [""] * len(cols)
    for ci, toks in best_slots.items():
        headers[ci] = " ".join(toks)
    # Stacked headers ("2024/25" over "Est.") fill only the slots still empty.
    if header_idx >= 0 and not all(headers):
        for ln in lines[header_idx + 1 : header_idx + 3]:
            if _numeric_count(ln) >= 2:
                break
            for ci, toks in _slots_for(ln).items():
                if not headers[ci]:
                    headers[ci] = " ".join(toks)
    headers = [h or f"col{i + 1}" for i, h in enumerate(headers)]

    rows: list[dict] = []
    for ln in lines[header_idx + 1 :]:
        if _numeric_count(ln) < 1:
            # A row with no values is a section banner -- "Growth (in percent)",
            # "Prices (percent change, period average)". Dropping it strips the unit and
            # the basis from every value underneath, so keep it as an unvalued marker.
            text = ln.text.strip()
            if len(text.split()) >= 2:
                rows.append({"label": text, "cells": {}, "bbox": ln.bbox,
                             "cell_boxes": [], "section": True})
            continue
        label_words = [w for w in ln.words if w.x0 < left_edge]
        cells: dict[str, str] = {}
        boxes: list[BBox] = []
        for w in ln.words:
            if w.x0 < left_edge:
                continue
            ci = col_of(w)
            if ci is None:
                continue
            # PDF word extraction splits decimals ("456." + "1"). Two fragments landing in
            # the same cell are one number, so rejoin them without a space.
            have = cells.get(headers[ci], "")
            glue = (
                ""
                if have.endswith((".", ","))
                or (have[-1:].isdigit() and w.text[:1].isdigit())
                else " "
            )
            cells[headers[ci]] = (have + glue + w.text).strip() if have else w.text
            boxes.append((w.x0, w.y0, w.x1, w.y1))
        if not cells:
            continue
        label = " ".join(w.text for w in label_words).strip()
        rows.append({"label": label, "cells": cells, "bbox": ln.bbox, "cell_boxes": boxes})

    # A label that wrapped onto its own line belongs to the row beneath it.
    for i, r in enumerate(rows):
        if not r["label"] and i > 0 and not r.get("section"):
            r["label"] = rows[i - 1]["label"]

    caption = " ".join(ln.text for ln in lines[: max(header_idx, 0)]).strip()
    return rows, caption[:300]


def render_table(rows: list[dict], caption: str = "") -> str:
    """Row-wise text. Every value carries its own header, so nothing depends on the
    reader tracking column positions across a wide table."""
    out = [caption] if caption else []
    for r in rows:
        if r.get("section"):
            # Section banners carry the unit and basis for every row beneath them —
            # "Growth (in percent)", "Balance of payments (in billions of U.S. dollars)".
            out.append(f"[{r['label']}]")
            continue
        pairs = "; ".join(f"{h}={v}" for h, v in r["cells"].items() if v)
        if pairs:
            out.append(f"{r['label'] or '(unlabelled row)'}: {pairs}")
    return "\n".join(out)


# ---------------------------------------------------------------- column detection
def detect_columns(lines: list[Line], page_width: float) -> list[tuple[float, float]]:
    """Find a central gutter splitting prose into two columns.

    Restricted to a gutter straddling the page centre with substantial text on both sides,
    so a table's internal whitespace is not mistaken for a column break.
    """
    if len(lines) < 8:
        return [(0.0, page_width)]
    bins = 200
    occupied = [False] * bins
    for ln in lines:
        x0, _, x1, _ = ln.bbox
        for b in range(
            max(0, int(x0 / page_width * bins)), min(bins, int(x1 / page_width * bins) + 1)
        ):
            occupied[b] = True

    best: tuple[int, int] | None = None
    b = 0
    while b < bins:
        if occupied[b]:
            b += 1
            continue
        start = b
        while b < bins and not occupied[b]:
            b += 1
        if start <= bins // 2 <= b and (b - start) >= bins * 0.025:
            if best is None or (b - start) > (best[1] - best[0]):
                best = (start, b)
    if not best:
        return [(0.0, page_width)]

    split = (best[0] + best[1]) / 2 / bins * page_width
    left = [ln for ln in lines if ln.bbox[2] <= split]
    right = [ln for ln in lines if ln.bbox[0] >= split]
    if len(left) < 5 or len(right) < 5:
        return [(0.0, page_width)]
    return [(0.0, split), (split, page_width)]


def cluster_slide_tiles(lines: list[Line], page_width: float) -> list[list[Line]]:
    """Group a slide's lines into the visual tiles a reader sees.

    Infographic slides put a figure above its caption and place several such tiles side by
    side. Reading the page in flat top-to-bottom order interleaves them, so
    "Rs.2,076 Cr | Rs.46 Cr | Rs.21 Cr" arrives as one line and the captions as another,
    and any reader — human or model — has to guess which caption belongs to which figure.
    That guess is where a Q4 EBITDA figure gets recorded as annual revenue.

    Clustering by horizontal position keeps each figure with its own caption.
    """
    if not lines:
        return []

    # Split each row at wide horizontal gaps first. Row banding has already merged the
    # three tiles' figures into a single line because they share a baseline, so clustering
    # whole lines can never separate them.
    min_gap = page_width * 0.035
    segments: list[Line] = []
    for ln in lines:
        current = [ln.words[0]]
        for prev, word in zip(ln.words, ln.words[1:]):
            if word.x0 - prev.x1 > min_gap:
                segments.append(Line(current, ln.size, ln.bold))
                current = [word]
            else:
                current.append(word)
        segments.append(Line(current, ln.size, ln.bold))

    full = [ln for ln in segments if (ln.bbox[2] - ln.bbox[0]) > page_width * 0.55]
    rest = [ln for ln in segments if ln not in full]

    if not rest:
        return [sorted(full, key=lambda l: l.yc)] if full else []

    # Cluster tiles by the x-centre of each segment. Growing a tile from overlapping
    # extents does not work: captions are wider than the figures above them, so one tile
    # widens until it swallows the page and the separation is lost again. Centres are
    # stable because each tile is laid out around its own axis.
    centres = sorted(( (l.bbox[0] + l.bbox[2]) / 2 for l in rest ))
    bounds: list[float] = []
    for prev, cur in zip(centres, centres[1:]):
        if cur - prev > page_width * 0.05:
            bounds.append((prev + cur) / 2)

    def tile_of(ln: Line) -> int:
        centre = (ln.bbox[0] + ln.bbox[2]) / 2
        return sum(1 for b in bounds if centre > b)

    grouped: dict[int, list[Line]] = {}
    for ln in rest:
        grouped.setdefault(tile_of(ln), []).append(ln)

    tiles = [grouped[k] for k in sorted(grouped)]
    for tile in tiles:
        tile.sort(key=lambda l: (l.yc, l.bbox[0]))

    banner = sorted(full, key=lambda l: l.yc)
    return ([banner] if banner else []) + tiles


# ---------------------------------------------------------------- prose reflow
def _dehyphenate(prev: str, nxt: str) -> str | None:
    """Join a word broken across a line, keeping the hyphen.

    A trailing hyphen is ambiguous: it is either a line-break hyphen ("produc-" + "tivity")
    or a genuine compound ("long-" + "run"), and nothing in the text distinguishes them
    without a dictionary. Dropping it fabricates non-words like "longrun"; keeping it leaves
    "produc-tivity", which is still readable and still matches on the numbers that matter.
    Keeping it is the failure that does less damage, so that is the default.
    """
    if prev.endswith("-") and nxt[:1].islower():
        return prev + nxt
    return None


def reconstruct_paragraphs(lines: list[Line], col_width: float) -> list[tuple[str, BBox]]:
    """Re-flow wrapped lines into paragraphs."""
    if not lines:
        return []
    gaps = [
        lines[i + 1].bbox[1] - lines[i].bbox[3] for i in range(len(lines) - 1)
    ]
    typical = statistics.median([g for g in gaps if g > -2] or [2.0])
    heights = statistics.median([ln.height for ln in lines] or [10.0])

    paras: list[list[Line]] = [[lines[0]]]
    for i in range(1, len(lines)):
        prev, cur = lines[i - 1], lines[i]
        gap = cur.bbox[1] - prev.bbox[3]
        breaks = (
            gap > max(typical + heights * 0.6, heights * 0.9)
            or bool(_PARA_MARKER.match(cur.text))
            # A line that stops well short of the column edge ended its paragraph.
            or (prev.bbox[2] - prev.bbox[0]) < col_width * 0.55
            or abs(cur.size - prev.size) > 1.2
        )
        (paras.append([cur]) if breaks else paras[-1].append(cur))

    out: list[tuple[str, BBox]] = []
    for group in paras:
        text = ""
        for ln in group:
            t = ln.text.strip()
            if not text:
                text = t
                continue
            joined = _dehyphenate(text, t)
            text = joined if joined else f"{text} {t}"
        bbox = (
            min(l.bbox[0] for l in group),
            min(l.bbox[1] for l in group),
            max(l.bbox[2] for l in group),
            max(l.bbox[3] for l in group),
        )
        if text.strip():
            out.append((" ".join(text.split()), bbox))
    return out
