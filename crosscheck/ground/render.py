"""Rendering a fact's evidence as an image of the page it came from.

Rendered lazily and cached on disk: a hundred-page PDF has no reason to become a hundred
PNGs when a reader will look at three of them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..config import REPO_ROOT, settings
from ..db import session
from ..ingest import pdf
from ..ingest.layout import Word
from .bbox import BBox, Located, locate


def document_path(doc_id: int) -> Path:
    """Where the ingested copy of a document lives.

    The stored path is absolute, baked in at ingest time on whatever machine did the
    ingesting -- which is never true again once a database file moves. The sample database
    committed with this submission is exactly that case: it lets a reader browse real,
    grounded facts with no API key, but its stored_path values point at a path on the
    machine that produced it. Falling back to a search of starter-datasets/ by filename
    (which ships in the repository alongside the sample) is what keeps evidence rendering
    working for anyone who opens it, rather than only for the machine that ingested it.
    """
    with session() as conn:
        row = conn.execute(
            "SELECT filename, meta_json FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"no document {doc_id}")

    stored = json.loads(row["meta_json"] or "{}").get("stored_path")
    if stored and Path(stored).exists():
        return Path(stored)

    for candidate in REPO_ROOT.glob(f"starter-datasets/**/{row['filename']}"):
        return candidate

    raise FileNotFoundError(
        f"no copy of {row['filename']!r} found (checked the recorded upload path and "
        f"starter-datasets/) -- place the PDF there to enable evidence rendering"
    )


def page_words(doc_id: int, page_no: int) -> list[Word]:
    import fitz

    with fitz.open(document_path(doc_id)) as doc:
        return pdf.read_page(doc[page_no], page_no).words


def evidence_rects(fact_id: int) -> tuple[Located, int, int]:
    """Locate one fact's evidence. Returns (located, doc_id, page_no)."""
    with session() as conn:
        row = conn.execute(
            """SELECT f.doc_id, f.evidence_page, f.evidence_quote, f.value_raw,
                      b.bbox_json
                 FROM facts f JOIN blocks b ON b.id = f.block_id
                WHERE f.id = ?""",
            (fact_id,),
        ).fetchone()
    if row is None:
        raise KeyError(f"no fact {fact_id}")

    bbox = json.loads(row["bbox_json"] or "null")
    located = locate(
        quote=row["evidence_quote"],
        value=row["value_raw"],
        words=page_words(row["doc_id"], row["evidence_page"]),
        block_bbox=tuple(bbox) if bbox else None,
    )
    return located, row["doc_id"], row["evidence_page"]


def render_evidence(fact_id: int, *, dpi: int = 130) -> Path:
    """Render the page with the fact's evidence highlighted, caching the result."""
    located, doc_id, page_no = evidence_rects(fact_id)
    key = hashlib.sha256(
        f"{doc_id}:{page_no}:{dpi}:{located.rects}".encode()
    ).hexdigest()[:20]

    settings.ensure_dirs()
    out = settings.data_dir / "pages" / f"{key}.png"
    if out.exists():
        return out

    png = pdf.render_page(
        document_path(doc_id), page_no, highlights=located.rects, dpi=dpi
    )
    out.write_bytes(png)
    return out


def render_plain(doc_id: int, page_no: int, *, dpi: int = 130) -> Path:
    key = hashlib.sha256(f"{doc_id}:{page_no}:{dpi}:plain".encode()).hexdigest()[:20]
    settings.ensure_dirs()
    out = settings.data_dir / "pages" / f"{key}.png"
    if not out.exists():
        out.write_bytes(pdf.render_page(document_path(doc_id), page_no, dpi=dpi))
    return out
