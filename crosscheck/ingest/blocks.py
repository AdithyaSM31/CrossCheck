"""Turning reconstructed layout into extraction units.

Blocks are sized for an extractor, not for a typesetter. Three decisions do most of the work:

1. **Tables carry their headers into every row.** A bare cell "9.2" is meaningless without
   the column it sits under, and positional alignment does not survive a shredded row label.
2. **Footnotes travel with the block.** This corpus qualifies a large share of its numbers in
   footnotes — "excluding revenue from traded goods", "as per RedSeer report basis FY21
   revenue". A number extracted without its footnote gets the wrong basis, and a wrong basis
   becomes a false contradiction two stages later.
3. **Blocks that cannot contain a fact never reach the model**, and small adjacent paragraphs
   are packed together. Both cut API calls, which is what makes a free-tier provider viable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .layout import (
    Line,
    build_lines,
    detect_columns,
    find_running_lines,
    find_table_regions,
    page_label,
    reconstruct_paragraphs,
    reconstruct_table,
    render_table,
    cluster_slide_tiles,
)
from .pdf import Page, sha256_text

MIN_BLOCK_CHARS = 25
PACK_TARGET = 5000  # characters per extraction unit
PACK_MAX = 7000

# Cues that a block states something factual without containing a digit — appointments,
# addresses, auditors, resignations. Grammatical cues, not facts about any one company.
_SEMANTIC_CUES = re.compile(
    r"\b(appointed|resigned|retired|re-?designated|ceased|inducted|elected|"
    r"registered office|corporate office|principal place|situated at|located at|"
    r"auditor|auditors|chairman|chairperson|managing director|director|"
    r"company secretary|incorporated|renamed|merged|acquired|subsidiary|"
    r"listed on|headquartered)\b",
    re.I,
)

_BOILERPLATE = re.compile(
    r"(forward[- ]looking statements|safe harbou?r|no part of it shall|"
    r"this presentation is prepared|without regards to specific objectives|"
    r"all rights reserved|shall not be reproduced)",
    re.I,
)


@dataclass
class Block:
    page_no: int
    ordinal: int
    kind: str  # paragraph | table
    text: str
    bbox: tuple[float, float, float, float]
    section_path: str = ""
    char_start: int = -1
    char_end: int = -1
    context: dict = field(default_factory=dict)
    extractable: bool = True
    skip_reason: str = ""

    @property
    def sha256(self) -> str:
        notes = "|".join(self.context.get("footnotes", []))
        return sha256_text(f"{self.kind}\n{self.section_path}\n{self.text}\n{notes}")

    def prompt_text(self) -> str:
        """What the model sees — and therefore what grounding verifies against."""
        parts = []
        if self.section_path:
            parts.append(f"[Section: {self.section_path}]")
        parts.append(self.text)
        for note in self.context.get("footnotes", []):
            parts.append(f"[Footnote on same page: {note}]")
        return "\n".join(parts)


# ---------------------------------------------------------------------------- headings
def _is_heading(line: Line, body_size: float, col_width: float) -> bool:
    """Strict, because a permissive test turns every short wrapped line into a heading and
    poisons the section path of everything beneath it."""
    text = line.text.strip()
    if not (3 <= len(text) <= 110):
        return False
    if text.endswith((",", ";", ":", "-", "and", "the", "of", "in")):
        return False
    if re.search(r"[.!?]$", text) and len(text) > 40:
        return False
    width = line.bbox[2] - line.bbox[0]
    if width > col_width * 0.85:  # a heading does not fill the column; a wrapped line does
        return False
    # A headline figure on a slide ("₹8,142 Cr", "1.4 Mn Tons") is short, large and does not
    # fill its column, so it passes every stylistic test for a heading. Being mostly digits
    # is what tells them apart — and a value swallowed into the section path is a value the
    # extractor never sees.
    # A line opening with a figure or a currency mark is a value, not a heading — the
    # ratio of digits alone is not enough ("1.4 Mn Tons" is only 18% digits). Numbered
    # headings ("1. Introduction") are kept by the second test. The costs are asymmetric:
    # demoting a heading to prose loses a section label, while promoting a value to a
    # heading deletes the value from the corpus entirely.
    if re.match(r"^[₹$€£]|^Rs\.?\s*\d|^\(?\d", text) and not re.match(
        r"^\d{1,2}[.)]\s+[A-Z]", text
    ):
        return False
    bigger = line.size >= body_size * 1.12
    styled = line.bold or (text.isupper() and len(text) > 4)
    return bool(bigger or (styled and len(text) < 70))


class _SectionStack:
    def __init__(self) -> None:
        self._stack: list[tuple[float, str]] = []

    def push(self, size: float, text: str) -> None:
        while self._stack and self._stack[-1][0] <= size:
            self._stack.pop()
        self._stack.append((size, text))
        del self._stack[:-3]

    def path(self) -> str:
        return " > ".join(t for _, t in self._stack)


# ---------------------------------------------------------------------------- footnotes
_FOOTNOTE_MARK = re.compile(r"^\s*(\(\d{1,2}\)|\d{1,2}[.)]|[*†‡])\s+\S")


def _footnotes(lines: list[Line], page_height: float, body_size: float) -> list[str]:
    notes: list[str] = []
    for ln in lines:
        low = ln.bbox[1] >= page_height * 0.70
        small = ln.size and ln.size <= body_size * 0.94
        if low and (small or _FOOTNOTE_MARK.match(ln.text)):
            t = ln.text.strip()
            if 8 <= len(t) <= 300:
                notes.append(" ".join(t.split()))
    return notes[:8]


# ---------------------------------------------------------------------------- prefilter
def classify(text: str, kind: str) -> tuple[bool, str]:
    stripped = text.strip()
    if _BOILERPLATE.search(stripped):
        return False, "boilerplate"
    if kind == "table":
        return True, ""
    if len(stripped) < MIN_BLOCK_CHARS:
        return False, "too short"
    if not re.search(r"\d", stripped) and not _SEMANTIC_CUES.search(stripped):
        return False, "no numeric or semantic content"
    return True, ""


# ---------------------------------------------------------------------------- packing
def _pack(items: list[tuple[str, tuple]], section: str) -> list[tuple[str, tuple]]:
    """Merge adjacent paragraphs up to a character budget.

    PDF paragraphs are frequently one or two sentences. Sending each as its own request
    wastes calls and, worse, strips the sentence that qualifies a number.
    """
    packed: list[tuple[str, tuple]] = []
    buf_text: list[str] = []
    buf_box: list[tuple] = []

    def flush() -> None:
        if not buf_text:
            return
        bbox = (
            min(b[0] for b in buf_box),
            min(b[1] for b in buf_box),
            max(b[2] for b in buf_box),
            max(b[3] for b in buf_box),
        )
        packed.append(("\n\n".join(buf_text), bbox))
        buf_text.clear()
        buf_box.clear()

    for text, bbox in items:
        size = sum(len(t) for t in buf_text) + len(text)
        if buf_text and size > PACK_TARGET:
            flush()
        buf_text.append(text)
        buf_box.append(bbox)
        if sum(len(t) for t in buf_text) >= PACK_MAX:
            flush()
    flush()
    return packed


# ---------------------------------------------------------------------------- entry point
def build_blocks(pages: list[Page]) -> list[Block]:
    page_lines = [build_lines(p.words) for p in pages]
    height = max((p.height for p in pages), default=792.0)
    furniture = find_running_lines(page_lines, height)

    out: list[Block] = []
    ordinal = 0
    stack = _SectionStack()

    for page, lines in zip(pages, page_lines):
        label = page_label(lines, page.height)
        lines = [
            ln for ln in lines if re.sub(r"\d+", "#", ln.text.strip()) not in furniture
        ]
        if not lines:
            continue

        notes = _footnotes(lines, page.height, page.body_size)
        note_set = {n for n in notes}

        # Tables first: they span the full width, so they must be carved out before prose
        # is split into columns.
        slide = page.width > page.height
        if slide:
            stack = _SectionStack()  # slide decks have no running section hierarchy

        taken: set[int] = set()
        for start, end in find_table_regions(lines):
            region = lines[start : end + 1]
            rows, caption = reconstruct_table(region, page.width)
            text = render_table(rows, caption) if rows else ""

            # A genuine table needs distinct columns. Slides need a higher bar: metric
            # tiles sitting side by side look like a two-column table, and accepting them
            # interleaves three unrelated figures into one row while dropping a fourth.
            # A real slide table (quarterly operating metrics) has three or more.
            columns = {h for r in rows for h in r["cells"]}
            if len(columns) < (3 if slide else 2) or len(text) < MIN_BLOCK_CHARS:
                continue

            # If most columns could not be named, the values are bound to nothing and the
            # whole premise of this representation is gone. That happens on chart slides
            # whose bar labels look like aligned numbers, and on regions that were never a
            # table. Passing them on produces facts like "= 12%" that ground perfectly and
            # mean nothing, and they go on to form confident false corroborations. Falling
            # back to the raw prose keeps the text without inventing structure for it.
            unnamed = sum(1 for h in columns if re.fullmatch(r"col\d+", h))
            if unnamed > len(columns) / 2:
                continue

            taken.update(range(start, end + 1))
            ok, why = classify(text, "table")
            bbox = (
                min(l.bbox[0] for l in region), min(l.bbox[1] for l in region),
                max(l.bbox[2] for l in region), max(l.bbox[3] for l in region),
            )
            out.append(
                Block(
                    page_no=page.page_no, ordinal=ordinal, kind="table", text=text,
                    bbox=bbox, section_path=stack.path(),
                    context={"footnotes": notes, "n_rows": len(rows), "page_label": label},
                    extractable=ok, skip_reason=why,
                )
            )
            ordinal += 1

        prose = [ln for i, ln in enumerate(lines) if i not in taken]
        prose = [ln for ln in prose if ln.text.strip() not in note_set]
        if not prose:
            continue

        # Presentation slides are a different medium. Values sit above their captions in an
        # infographic grid, so paragraph re-flow and column splitting both scramble them.
        # A slide holds little enough text that the whole page is the right extraction unit,
        # and it gives the model the caption next to the number in a single call — but the
        # tiles have to be separated first, or the figures arrive on one line and their
        # captions on the next and the model has to guess which belongs to which.
        if slide:
            tiles = cluster_slide_tiles(prose, page.width)
            text = "\n\n".join(
                "\n".join(ln.text for ln in tile) for tile in tiles if tile
            ).strip()
            if len(text) >= MIN_BLOCK_CHARS:
                ok, why = classify(text, "paragraph")
                out.append(
                    Block(
                        page_no=page.page_no, ordinal=ordinal, kind="paragraph", text=text,
                        bbox=(
                            min(l.bbox[0] for l in prose), min(l.bbox[1] for l in prose),
                            max(l.bbox[2] for l in prose), max(l.bbox[3] for l in prose),
                        ),
                        section_path="",
                        context={"footnotes": notes, "page_label": label, "slide": True},
                        extractable=ok, skip_reason=why,
                    )
                )
                ordinal += 1
            continue

        for cx0, cx1 in detect_columns(prose, page.width):
            col_width = cx1 - cx0
            col_lines = [
                ln for ln in prose if ln.bbox[0] >= cx0 - 1 and ln.bbox[2] <= cx1 + 1
            ]
            if not col_lines:
                continue

            # Split out headings so they set the section path instead of becoming facts.
            runs: list[list[Line]] = [[]]
            for ln in col_lines:
                if _is_heading(ln, page.body_size, col_width):
                    stack.push(ln.size or page.body_size, " ".join(ln.text.split()))
                    runs.append([])
                else:
                    runs[-1].append(ln)

            section_now = stack.path()
            paras: list[tuple[str, tuple]] = []
            for run in runs:
                paras.extend(reconstruct_paragraphs(run, col_width))

            for text, bbox in _pack(paras, section_now):
                ok, why = classify(text, "paragraph")
                start = page.text.find(text[:60]) if text else -1
                out.append(
                    Block(
                        page_no=page.page_no, ordinal=ordinal, kind="paragraph", text=text,
                        bbox=bbox, section_path=section_now,
                        char_start=start,
                        char_end=start + len(text) if start >= 0 else -1,
                        context={"footnotes": notes, "page_label": label},
                        extractable=ok, skip_reason=why,
                    )
                )
                ordinal += 1

    return out
