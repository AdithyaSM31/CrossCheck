"""HTTP API and the web UI it serves.

Upload a PDF and the whole pipeline runs against it: ingest, extract, ground, consolidate
the vocabulary, reconcile. Progress is reported through a job record so the page can show
what is happening during the minutes that takes.

Every endpoint returns evidence alongside claims. That is the point of the system, so it is
not an option the caller has to ask for.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..config import settings
from ..db import session
from ..ingest.pipeline import ingest

WEB = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="CrossCheck", version="0.1")


# --------------------------------------------------------------------------- jobs
def _set_job(job_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with session() as conn:
        conn.execute(
            f"UPDATE jobs SET {cols}, updated_at = datetime('now') WHERE id = ?",
            (*fields.values(), job_id),
        )


async def _run_pipeline(job_id: str, path: Path) -> None:
    """Ingest, extract, consolidate, reconcile — the whole layer for one document."""
    from ..extract.extractor import extract_document, infer_metadata
    from ..link.attributes import consolidate
    from ..llm.client import LLMClient
    from ..reason.adjudicate import reconcile

    try:
        _set_job(job_id, stage="ingesting", message="reading the PDF")
        result = await asyncio.to_thread(ingest, path)
        _set_job(job_id, doc_id=result.doc_id, total=result.extractable)

        if not settings.extract.configured:
            _set_job(
                job_id, stage="done",
                message="ingested; no extraction model configured (set CROSSCHECK_EXTRACT_API_KEY)",
            )
            return

        extractor = LLMClient(
            settings.extract, concurrency=settings.concurrency,
            timeout=settings.request_timeout, extra_body=settings.extract.extra_body(),
            tokens_per_minute=settings.tokens_per_minute,
        )
        try:
            _set_job(job_id, stage="identifying", message="identifying the document")
            await infer_metadata(result.doc_id, extractor)

            def progress(stage: str, done: int, total: int) -> None:
                _set_job(job_id, stage="extracting", done=done, total=total)

            _set_job(job_id, stage="extracting", message="extracting and grounding facts")
            stats = await extract_document(
                result.doc_id, progress=progress, client=extractor
            )
            _set_job(job_id, message=stats.summary())
        finally:
            await extractor.aclose()

        reasoner = (
            LLMClient(
                settings.reason, concurrency=settings.concurrency,
                timeout=settings.request_timeout,
                extra_body=settings.reason.extra_body(),
                tokens_per_minute=settings.tokens_per_minute,
            )
            if settings.reason.configured
            else None
        )
        try:
            _set_job(job_id, stage="linking", message="consolidating the vocabulary")
            await consolidate(reasoner, use_llm=reasoner is not None)

            _set_job(job_id, stage="reconciling", message="comparing against known facts")
            rec = await reconcile(reasoner, max_llm_calls=120)
            _set_job(job_id, stage="done", message=rec.summary())
        finally:
            if reasoner:
                await reasoner.aclose()

    except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not swallowed
        _set_job(job_id, stage="failed", error=str(exc)[:500])


@app.post("/api/documents")
async def upload(background: BackgroundTasks, file: UploadFile) -> JSONResponse:
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "only PDF files are accepted")

    settings.ensure_dirs()
    target = settings.data_dir / "uploads" / f"{uuid.uuid4().hex[:12]}_{file.filename}"
    target.write_bytes(await file.read())

    job_id = uuid.uuid4().hex[:12]
    with session() as conn:
        conn.execute(
            "INSERT INTO jobs (id, stage, message) VALUES (?, 'queued', ?)",
            (job_id, f"queued {file.filename}"),
        )
    background.add_task(_run_pipeline, job_id, target)
    return JSONResponse({"job_id": job_id}, status_code=202)


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> dict:
    with session() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such job")
    return dict(row)


# --------------------------------------------------------------------------- reads
@app.get("/api/documents")
def documents() -> list[dict]:
    with session() as conn:
        return [
            dict(r)
            for r in conn.execute(
                """SELECT d.id, d.filename, d.title, d.publisher, d.doc_type,
                          d.published_on, d.page_count, d.status,
                          (SELECT COUNT(*) FROM blocks b WHERE b.doc_id = d.id) blocks,
                          (SELECT COUNT(*) FROM facts f WHERE f.doc_id = d.id) facts,
                          (SELECT COUNT(*) FROM rejected_facts r WHERE r.doc_id = d.id) rejected
                     FROM documents d ORDER BY d.id"""
            )
        ]


@app.get("/api/stats")
def stats() -> dict:
    with session() as conn:
        one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
        return {
            "documents": one("SELECT COUNT(*) FROM documents"),
            "facts": one("SELECT COUNT(*) FROM facts"),
            "attributes": one("SELECT COUNT(*) FROM attributes"),
            "relations": one("SELECT COUNT(*) FROM relations"),
            "rejected": one("SELECT COUNT(*) FROM rejected_facts"),
            "by_type": {
                r["type"]: r["n"]
                for r in conn.execute(
                    "SELECT type, COUNT(*) n FROM relations GROUP BY type"
                )
            },
        }


@app.get("/api/facts")
def facts(
    q: str | None = None,
    doc: int | None = None,
    attribute: str | None = None,
    period: str | None = None,
    min_confidence: float = 0.0,
    limit: int = Query(100, le=500),
    offset: int = 0,
) -> dict:
    FROM = """
        FROM facts f
        JOIN documents d ON d.id = f.doc_id
        LEFT JOIN attributes a ON a.id = f.attribute_id
    """
    where, params = ["f.confidence >= ?"], [min_confidence]
    if doc:
        where.append("f.doc_id = ?")
        params.append(doc)
    if attribute:
        where.append("COALESCE(a.canon_name, f.attribute_raw) = ?")
        params.append(attribute)
    if period:
        where.append("f.period_label = ?")
        params.append(period)
    if q:
        where.append(
            "(f.subject LIKE ? OR f.attribute_raw LIKE ? OR f.value_raw LIKE ?"
            " OR f.evidence_quote LIKE ?)"
        )
        params += [f"%{q}%"] * 4
    clause = " WHERE " + " AND ".join(where)
    with session() as conn:
        total = conn.execute(f"SELECT COUNT(*) {FROM} {clause}", params).fetchone()[0]
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT f.*, COALESCE(a.canon_name, f.attribute_raw) attribute,"
                " COALESCE(d.title, d.filename) doc_title, d.publisher"
                f" {FROM} {clause} ORDER BY f.id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        ]
    return {"total": total, "facts": rows}


@app.get("/api/facts/{fact_id}")
def fact_detail(fact_id: int) -> dict:
    with session() as conn:
        row = conn.execute(
            """SELECT f.*, COALESCE(a.canon_name, f.attribute_raw) attribute,
                      COALESCE(d.title, d.filename) doc_title, d.publisher, d.published_on
                 FROM facts f
                 JOIN documents d ON d.id = f.doc_id
                 LEFT JOIN attributes a ON a.id = f.attribute_id
                WHERE f.id = ?""",
            (fact_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "no such fact")
        related = [
            dict(r)
            for r in conn.execute(
                """SELECT r.*,
                          o.id other_id, o.subject other_subject, o.value_raw other_value,
                          o.period_label other_period, o.evidence_quote other_quote,
                          o.evidence_page other_page,
                          COALESCE(od.title, od.filename) other_doc
                     FROM relations r
                     JOIN facts o ON o.id = CASE WHEN r.fact_a = ? THEN r.fact_b ELSE r.fact_a END
                     JOIN documents od ON od.id = o.doc_id
                    WHERE r.fact_a = ? OR r.fact_b = ?
                    ORDER BY r.confidence DESC""",
                (fact_id, fact_id, fact_id),
            )
        ]
    return {"fact": dict(row), "relations": related}


@app.get("/api/relations")
def relations(
    type: str | None = None,
    cross_document: bool = False,
    limit: int = Query(50, le=300),
    offset: int = 0,
) -> dict:
    sql = [
        """SELECT r.*,
                  a.id a_id, a.subject a_subject, a.value_raw a_value,
                  a.period_label a_period, a.scope a_scope, a.basis a_basis,
                  a.evidence_quote a_quote, a.evidence_page a_page, a.doc_id a_doc,
                  COALESCE(da.title, da.filename) a_doc_title, da.publisher a_publisher,
                  b.id b_id, b.subject b_subject, b.value_raw b_value,
                  b.period_label b_period, b.scope b_scope, b.basis b_basis,
                  b.evidence_quote b_quote, b.evidence_page b_page, b.doc_id b_doc,
                  COALESCE(db.title, db.filename) b_doc_title, db.publisher b_publisher,
                  COALESCE(at.canon_name, a.attribute_raw) attribute
             FROM relations r
             JOIN facts a ON a.id = r.fact_a
             JOIN facts b ON b.id = r.fact_b
             JOIN documents da ON da.id = a.doc_id
             JOIN documents db ON db.id = b.doc_id
             LEFT JOIN attributes at ON at.id = a.attribute_id"""
    ]
    where, params = [], []
    if type:
        where.append("r.type = ?")
        params.append(type.upper())
    if cross_document:
        where.append("a.doc_id != b.doc_id")
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append("ORDER BY r.confidence DESC, r.id LIMIT ? OFFSET ?")
    params += [limit, offset]

    with session() as conn:
        rows = [dict(r) for r in conn.execute(" ".join(sql), params)]
        counts = {
            r["type"]: r["n"]
            for r in conn.execute("SELECT type, COUNT(*) n FROM relations GROUP BY type")
        }
    return {"counts": counts, "relations": rows}


@app.get("/api/schema")
def schema() -> dict:
    """The attribute vocabulary, which grows as documents introduce new measures."""
    with session() as conn:
        rows = []
        for r in conn.execute(
            "SELECT * FROM attributes ORDER BY n_facts DESC, canon_name"
        ):
            d = dict(r)
            d["aliases"] = [
                a for a in json.loads(d.pop("aliases_json") or "[]") if a != d["canon_name"]
            ]
            rows.append(d)
    return {"total": len(rows), "attributes": rows}


@app.get("/api/review")
def review(limit: int = Query(100, le=500)) -> dict:
    """Everything the system declined to believe, and why."""
    with session() as conn:
        reasons = [
            dict(r)
            for r in conn.execute(
                """SELECT reason, COUNT(*) n FROM rejected_facts
                   GROUP BY reason ORDER BY n DESC"""
            )
        ]
        items = []
        for r in conn.execute(
            """SELECT rf.*, COALESCE(d.title, d.filename) doc_title, b.page_no
                 FROM rejected_facts rf
                 LEFT JOIN documents d ON d.id = rf.doc_id
                 LEFT JOIN blocks b ON b.id = rf.block_id
                ORDER BY rf.id DESC LIMIT ?""",
            (limit,),
        ):
            d = dict(r)
            try:
                d["payload"] = json.loads(d.pop("payload_json") or "{}")
            except json.JSONDecodeError:
                d["payload"] = {}
            items.append(d)
        low = [
            dict(r)
            for r in conn.execute(
                """SELECT f.id, f.subject, f.attribute_raw, f.value_raw, f.confidence,
                          f.grounding, f.evidence_quote, f.evidence_page,
                          COALESCE(d.title, d.filename) doc_title
                     FROM facts f JOIN documents d ON d.id = f.doc_id
                    WHERE f.confidence < 0.6 OR f.grounding = 'fuzzy'
                    ORDER BY f.confidence LIMIT 60"""
            )
        ]
    return {"reasons": reasons, "rejected": items, "low_confidence": low}


@app.get("/api/facts/{fact_id}/evidence.png")
def evidence_image(fact_id: int) -> Response:
    """The source page with the verified evidence highlighted."""
    from ..ground.render import render_evidence

    try:
        path = render_evidence(fact_id)
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type="image/png")


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "extract": settings.extract.describe() if settings.extract.configured else None,
        "reason": settings.reason.describe() if settings.reason.configured else None,
    }


if WEB.exists():
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
