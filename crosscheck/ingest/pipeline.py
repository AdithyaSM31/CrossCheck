"""Ingest a PDF into the store: pages, blocks, and a document record.

Ingest is idempotent and content-addressed. Re-uploading the same PDF returns the existing
document instead of duplicating it, which is what makes incremental ingest of a growing
corpus cheap.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import settings
from ..db import js, session
from . import pdf
from .blocks import Block, build_blocks

Progress = Callable[[str, int, int], None]


@dataclass
class IngestResult:
    doc_id: int
    filename: str
    pages: int
    blocks: int
    extractable: int
    skipped: int
    scanned_pages: int
    already_present: bool

    @property
    def prefilter_saving(self) -> float:
        return 0.0 if not self.blocks else self.skipped / self.blocks


def ingest(
    path: Path, *, progress: Progress | None = None, store_copy: bool = True
) -> IngestResult:
    path = Path(path)
    settings.ensure_dirs()
    digest = pdf.sha256_file(path)

    def report(stage: str, done: int, total: int) -> None:
        if progress:
            progress(stage, done, total)

    with session() as conn:
        row = conn.execute(
            "SELECT id, filename, page_count FROM documents WHERE sha256 = ?", (digest,)
        ).fetchone()
        if row:
            counts = conn.execute(
                "SELECT COUNT(*) n, SUM(extractable) e FROM blocks WHERE doc_id = ?",
                (row["id"],),
            ).fetchone()
            n, e = counts["n"] or 0, counts["e"] or 0
            report("cached", 1, 1)
            return IngestResult(
                row["id"], row["filename"], row["page_count"], n, e, n - e, 0, True
            )

    report("reading", 0, 1)
    pages = pdf.read_document(path)
    scanned = [p.page_no for p in pages if pdf.text_density(p) < 0.5]

    report("blocking", 0, len(pages))
    blocks: list[Block] = build_blocks(pages)

    stored = path
    if store_copy:
        stored = settings.data_dir / "uploads" / f"{digest[:16]}_{path.name}"
        if not stored.exists():
            shutil.copy2(path, stored)

    with session() as conn:
        cur = conn.execute(
            """INSERT INTO documents (sha256, filename, page_count, status, meta_json)
               VALUES (?, ?, ?, 'ingested', ?)""",
            (
                digest,
                path.name,
                len(pages),
                js({"stored_path": str(stored), "scanned_pages": scanned}),
            ),
        )
        doc_id = int(cur.lastrowid)

        conn.executemany(
            "INSERT INTO pages (doc_id, page_no, text, width, height) VALUES (?,?,?,?,?)",
            [(doc_id, p.page_no, p.text, p.width, p.height) for p in pages],
        )
        conn.executemany(
            """INSERT INTO blocks
               (doc_id, page_no, ordinal, kind, text, char_start, char_end,
                bbox_json, section_path, context_json, sha256, extractable)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    doc_id, b.page_no, b.ordinal, b.kind, b.text, b.char_start, b.char_end,
                    js(list(b.bbox)), b.section_path,
                    js({**b.context, "skip_reason": b.skip_reason}),
                    b.sha256, int(b.extractable),
                )
                for b in blocks
            ],
        )

    extractable = sum(1 for b in blocks if b.extractable)
    report("done", len(pages), len(pages))
    return IngestResult(
        doc_id=doc_id,
        filename=path.name,
        pages=len(pages),
        blocks=len(blocks),
        extractable=extractable,
        skipped=len(blocks) - extractable,
        scanned_pages=len(scanned),
        already_present=False,
    )
