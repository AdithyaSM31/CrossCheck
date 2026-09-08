"""PDF reading: words, page rendering, and file hashing.

Structure recovery lives in ``layout.py``; this module only gets bytes off the page.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF

from .layout import Word


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean(s: str) -> str:
    return " ".join((s or "").replace("ﬁ", "fi").replace("ﬂ", "fl").split())


@dataclass
class Page:
    page_no: int
    text: str
    width: float
    height: float
    words: list[Word] = field(default_factory=list)
    body_size: float = 10.0


def read_page(page: fitz.Page, page_no: int) -> Page:
    # get_text("words") gives word boxes but no fonts; get_text("dict") gives fonts but no
    # word boxes. Both index lines the same way, so join them on (block, line).
    styles: dict[tuple[int, int], tuple[float, bool]] = {}
    sizes: list[float] = []
    for bi, b in enumerate(page.get_text("dict").get("blocks", [])):
        if b.get("type") != 0:
            continue
        for li, ln in enumerate(b.get("lines", [])):
            spans = ln.get("spans", [])
            if not spans:
                continue
            size = max(sp.get("size", 0.0) for sp in spans)
            bold = any("bold" in (sp.get("font", "") or "").lower() for sp in spans)
            styles[(bi, li)] = (size, bold)
            sizes.extend(sp.get("size", 0.0) for sp in spans)

    words = []
    for w in page.get_text("words"):
        text = clean(w[4])
        if not text:
            continue
        size, bold = styles.get((int(w[5]), int(w[6])), (0.0, False))
        words.append(Word(w[0], w[1], w[2], w[3], text, size, bold))

    sizes.sort()
    return Page(
        page_no=page_no,
        text=page.get_text(),
        width=page.rect.width,
        height=page.rect.height,
        words=words,
        body_size=sizes[len(sizes) // 2] if sizes else 10.0,
    )


def read_document(path: Path) -> list[Page]:
    with fitz.open(path) as doc:
        return [read_page(doc[i], i) for i in range(doc.page_count)]


def page_count(path: Path) -> int:
    with fitz.open(path) as doc:
        return doc.page_count


def text_density(page: Page) -> float:
    """Characters per thousand square points. Near zero means a scanned page."""
    area = max(page.width * page.height, 1.0)
    return len(page.text) / (area / 1000.0)


def render_page(
    path: Path,
    page_no: int,
    *,
    highlights: list[tuple[float, float, float, float]] | None = None,
    dpi: int = 130,
) -> bytes:
    """Render one page to PNG, optionally with evidence rectangles drawn on it."""
    with fitz.open(path) as doc:
        page = doc[page_no]
        for rect in highlights or []:
            annot = page.add_rect_annot(fitz.Rect(*rect))
            annot.set_colors(stroke=(0.85, 0.35, 0.0), fill=(1.0, 0.86, 0.4))
            annot.set_opacity(0.35)
            annot.set_border(width=1.2)
            annot.update()
        return page.get_pixmap(dpi=dpi).tobytes("png")
