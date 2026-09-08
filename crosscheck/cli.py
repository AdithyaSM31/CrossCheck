"""CrossCheck command line.

    python -m crosscheck.cli ingest "starter-datasets/delhivery/*.pdf"
    python -m crosscheck.cli docs
    python -m crosscheck.cli blocks 1 --page 4 --full
    python -m crosscheck.cli extract 3 --limit 10
    python -m crosscheck.cli facts --query revenue
    python -m crosscheck.cli review
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import settings
from .db import rejection_summary, session
from .ingest.pipeline import ingest


def _cmd_ingest(args: argparse.Namespace) -> int:
    paths: list[Path] = []
    for pattern in args.paths:
        p = Path(pattern)
        paths.extend(sorted(p.parent.glob(p.name)) if any(c in pattern for c in "*?") else [p])

    if not paths:
        print("no files matched", file=sys.stderr)
        return 1

    for path in paths:
        if not path.exists():
            print(f"  ! {path} does not exist", file=sys.stderr)
            continue
        res = ingest(path)
        tag = "cached" if res.already_present else "ingested"
        print(
            f"[{tag}] doc {res.doc_id:>3}  {res.filename}\n"
            f"        {res.pages} pages, {res.blocks} blocks, "
            f"{res.extractable} extractable "
            f"({res.prefilter_saving:.0%} prefiltered)"
            + (f", {res.scanned_pages} low-text pages" if res.scanned_pages else "")
        )
    return 0


def _cmd_docs(_: argparse.Namespace) -> int:
    with session() as conn:
        rows = conn.execute(
            """SELECT d.id, d.filename, d.title, d.publisher, d.published_on,
                      d.page_count, d.status,
                      COUNT(b.id) blocks, COALESCE(SUM(b.extractable), 0) extractable,
                      (SELECT COUNT(*) FROM facts f WHERE f.doc_id = d.id) facts
                 FROM documents d LEFT JOIN blocks b ON b.doc_id = d.id
                GROUP BY d.id ORDER BY d.id"""
        ).fetchall()
    if not rows:
        print("no documents ingested yet")
        return 0
    for r in rows:
        print(f"[{r['id']}] {r['filename']}")
        if r["title"]:
            print(f"     {r['title']}")
            print(f"     {r['publisher'] or '?'} | published {r['published_on'] or '?'}")
        print(
            f"     {r['page_count']} pages | {r['blocks']} blocks "
            f"({r['extractable']} extractable) | {r['facts']} facts | {r['status']}"
        )
    return 0


def _cmd_blocks(args: argparse.Namespace) -> int:
    sql = "SELECT * FROM blocks WHERE doc_id = ?"
    params: list = [args.doc_id]
    if args.page is not None:
        sql += " AND page_no = ?"
        params.append(args.page)
    if args.extractable_only:
        sql += " AND extractable = 1"
    sql += " ORDER BY ordinal LIMIT ?"
    params.append(args.limit)

    with session() as conn:
        rows = conn.execute(sql, params).fetchall()
    for r in rows:
        flag = "" if r["extractable"] else "  [SKIPPED]"
        print(
            f"\n--- block {r['id']} p{r['page_no']} {r['kind']}{flag} "
            f"[{r['section_path'] or '-'}]"
        )
        text = r["text"]
        print(text if args.full else text[:600] + ("..." if len(text) > 600 else ""))
    print(f"\n{len(rows)} block(s)")
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    from .extract.extractor import extract_document, infer_metadata
    from .llm.client import LLMClient

    with session() as conn:
        if args.doc_id:
            ids = [args.doc_id]
        else:
            ids = [r["id"] for r in conn.execute("SELECT id FROM documents ORDER BY id")]
    if not ids:
        print("no documents ingested yet", file=sys.stderr)
        return 1

    print(f"using {settings.extract.describe()}")

    async def run() -> None:
        client = LLMClient(
            settings.extract,
            concurrency=args.concurrency or settings.concurrency,
            timeout=settings.request_timeout,
            max_calls=args.max_calls or settings.max_calls,
            extra_body=settings.extract.extra_body(),
            tokens_per_minute=settings.tokens_per_minute,
        )
        try:
            for doc_id in ids:
                with session() as conn:
                    doc = conn.execute(
                        "SELECT filename, title FROM documents WHERE id = ?", (doc_id,)
                    ).fetchone()
                print(f"\ndoc {doc_id}  {doc['filename']}")

                if not doc["title"] and not args.no_metadata:
                    meta = await infer_metadata(doc_id, client)
                    if meta:
                        print(
                            f"  identified: {meta.get('title')} "
                            f"({meta.get('publisher')}, {meta.get('published_on')})"
                        )

                def progress(stage: str, done: int, total: int) -> None:
                    print(f"\r  {stage}: {done}/{total}", end="", flush=True)

                stats = await extract_document(
                    doc_id, limit=args.limit, kind=args.kind,
                    progress=progress, client=client,
                )
                print(f"\r  {stats.summary()}")
        finally:
            print(f"\nmodel usage -- {client.usage.summary()}")
            await client.aclose()

    asyncio.run(run())
    return 0


def _cmd_facts(args: argparse.Namespace) -> int:
    sql = ["SELECT f.*, d.filename FROM facts f JOIN documents d ON d.id = f.doc_id"]
    where, params = [], []
    if args.doc_id:
        where.append("f.doc_id = ?")
        params.append(args.doc_id)
    if args.query:
        where.append("(f.attribute_raw LIKE ? OR f.subject LIKE ?)")
        params += [f"%{args.query}%", f"%{args.query}%"]
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append("ORDER BY f.id LIMIT ?")
    params.append(args.limit)

    with session() as conn:
        rows = conn.execute(" ".join(sql), params).fetchall()
    for r in rows:
        print(f"\n[{r['id']}] {r['subject']} | {r['attribute_raw']} = {r['value_raw']}")
        bits = [b for b in (r["period_label"], r["scope"], r["basis"]) if b]
        print(f"      {' | '.join(bits) if bits else '(no qualifiers)'}")
        print(
            f"      p{r['evidence_page'] + 1} {r['grounding']} "
            f"({r['grounding_score']:.0f}) conf={r['confidence']:.2f}"
        )
        quote = r["evidence_quote"][:150].replace("\n", " ")
        print(f'      "{quote}"')
    print(f"\n{len(rows)} fact(s)")
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    with session() as conn:
        print("== rejection reasons ==")
        for r in rejection_summary(conn):
            print(f"  {r['n']:>5}  {r['reason']:<14} {r['detail']}")
        print("\n== sample rejections ==")
        for r in conn.execute(
            "SELECT * FROM rejected_facts ORDER BY id DESC LIMIT ?", (args.limit,)
        ):
            print(f"\n[{r['id']}] {r['reason']}: {r['detail']}")
            print(f"   {r['payload_json'][:300]}")
    return 0


def _reason_client(args: argparse.Namespace):
    from .llm.client import LLMClient

    return LLMClient(
        settings.reason,
        concurrency=args.concurrency or settings.concurrency,
        timeout=settings.request_timeout,
        max_calls=args.max_calls or settings.max_calls,
        extra_body=settings.reason.extra_body(),
        tokens_per_minute=settings.tokens_per_minute,
    )


def _cmd_link(args: argparse.Namespace) -> int:
    """Consolidate the attribute vocabulary and rebuild claim keys."""
    from .link.attributes import consolidate

    print(f"using {settings.reason.describe()}" if not args.no_llm else "using rules only")

    async def run() -> None:
        client = None if args.no_llm else _reason_client(args)
        try:
            stats = await consolidate(client, use_llm=not args.no_llm)
            print(stats.summary())
        finally:
            if client:
                print(f"model usage -- {client.usage.summary()}")
                await client.aclose()

    asyncio.run(run())
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    from .reason.adjudicate import reconcile

    print(f"using {settings.reason.describe()}" if not args.no_llm else "using rules only")

    async def run() -> None:
        client = None if args.no_llm else _reason_client(args)
        try:
            def progress(stage: str, done: int, total: int) -> None:
                print(f"\r  {stage}: {done}/{total}", end="", flush=True)

            stats = await reconcile(
                client, max_llm_calls=args.max_adjudications, progress=progress
            )
            print(f"\r{stats.summary()}")
        finally:
            if client:
                print(f"model usage -- {client.usage.summary()}")
                await client.aclose()

    asyncio.run(run())
    return 0


def _cmd_relations(args: argparse.Namespace) -> int:
    sql = """SELECT r.*,
                    a.subject sa, a.value_raw va, a.period_label pa, a.evidence_quote qa,
                    a.evidence_page ga, COALESCE(da.publisher, da.filename) da_name,
                    b.subject sb, b.value_raw vb, b.period_label pb, b.evidence_quote qb,
                    b.evidence_page gb, COALESCE(db.publisher, db.filename) db_name,
                    COALESCE(at.canon_name, a.attribute_raw) attr
               FROM relations r
               JOIN facts a ON a.id = r.fact_a
               JOIN facts b ON b.id = r.fact_b
               JOIN documents da ON da.id = a.doc_id
               JOIN documents db ON db.id = b.doc_id
               LEFT JOIN attributes at ON at.id = a.attribute_id"""
    where, params = [], []
    if args.type:
        where.append("r.type = ?")
        params.append(args.type.upper())
    else:
        # UNRELATED is bookkeeping that stops reconcile re-paying to ask about a pair it
        # already settled -- not a finding, so it stays out of an unfiltered listing.
        where.append("r.type != 'UNRELATED'")
    if args.query:
        where.append(
            "(a.subject LIKE ? OR COALESCE(at.canon_name, a.attribute_raw) LIKE ?"
            " OR a.value_raw LIKE ? OR b.value_raw LIKE ? OR r.explanation LIKE ?)"
        )
        params += [f"%{args.query}%"] * 5
    if args.cross_document:
        where.append("a.doc_id != b.doc_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.confidence DESC LIMIT ?"
    params.append(args.limit)

    with session() as conn:
        rows = conn.execute(sql, params).fetchall()
        if not args.type:
            print("== relation counts ==")
            for r in conn.execute(
                """SELECT type, decided_by, COUNT(*) n FROM relations
                   GROUP BY type, decided_by ORDER BY n DESC"""
            ):
                print(f"  {r['n']:>5}  {r['type']:<22} by {r['decided_by']}")
            print()

    for r in rows:
        mark = "x" if r["type"] == "CONTRADICTS" else "="
        print(f"\n[{mark}] {r['type']}"
              + (f"  ({r['discriminator']})" if r["discriminator"] else "")
              + f"  conf={r['confidence']:.2f}  by {r['decided_by']}")
        print(f"    {r['sa']} — {r['attr']}")
        print(f"      A  {r['va']}  [{r['pa'] or 'no period'}]  {r['da_name']} p{r['ga'] + 1}")
        print(f"         \"{(r['qa'] or '')[:130]}\"")
        print(f"      B  {r['vb']}  [{r['pb'] or 'no period'}]  {r['db_name']} p{r['gb'] + 1}")
        print(f"         \"{(r['qb'] or '')[:130]}\"")
        if r["explanation"]:
            print(f"      -> {r['explanation']}")
    print(f"\n{len(rows)} relation(s)")
    return 0


def _cmd_schema(args: argparse.Namespace) -> int:
    """The attribute vocabulary as it currently stands."""
    with session() as conn:
        rows = conn.execute(
            """SELECT canon_name, aliases_json, unit_family, n_facts
                 FROM attributes ORDER BY n_facts DESC LIMIT ?""",
            (args.limit,),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) n FROM attributes").fetchone()["n"]
    print(f"{total} canonical attributes discovered\n")
    for r in rows:
        aliases = json.loads(r["aliases_json"] or "[]")
        extra = [a for a in aliases if a != r["canon_name"]]
        print(f"  {r['n_facts']:>5}  {r['canon_name']}  [{r['unit_family'] or '?'}]")
        if extra:
            print(f"         aka: {', '.join(extra[:6])}")
    return 0


def _cmd_evidence(args: argparse.Namespace) -> int:
    from .ground.render import evidence_rects, render_evidence

    with session() as conn:
        row = conn.execute(
            """SELECT f.*, d.filename FROM facts f JOIN documents d ON d.id = f.doc_id
                WHERE f.id = ?""",
            (args.fact_id,),
        ).fetchone()
    if row is None:
        print(f"no fact {args.fact_id}", file=sys.stderr)
        return 1

    print(f"{row['subject']} | {row['attribute_raw']} = {row['value_raw']}")
    print(f"  {row['filename']} page {row['evidence_page'] + 1}")
    print(f"  grounding: {row['grounding']} ({row['grounding_score']:.0f})")
    print(f"  quote: \"{row['evidence_quote'][:200]}\"")

    located, _, _ = evidence_rects(args.fact_id)
    print(f"  located by: {located.how} ({len(located.rects)} region(s))")
    path = render_evidence(args.fact_id)
    print(f"  rendered: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crosscheck")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="read PDFs into the store")
    p.add_argument("paths", nargs="+")
    p.set_defaults(fn=_cmd_ingest)

    p = sub.add_parser("docs", help="list ingested documents")
    p.set_defaults(fn=_cmd_docs)

    p = sub.add_parser("blocks", help="inspect extraction units")
    p.add_argument("doc_id", type=int)
    p.add_argument("--page", type=int)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--full", action="store_true")
    p.add_argument("--extractable-only", action="store_true")
    p.set_defaults(fn=_cmd_blocks)

    p = sub.add_parser("extract", help="extract facts with the configured model")
    p.add_argument("doc_id", type=int, nargs="?")
    p.add_argument("--limit", type=int, default=0, help="blocks per document (0 = all)")
    p.add_argument("--kind", choices=["table", "paragraph"],
                   help="restrict to one block kind (e.g. re-extract only tables)")
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--max-calls", type=int, default=0, help="hard ceiling on model calls")
    p.add_argument("--no-metadata", action="store_true")
    p.set_defaults(fn=_cmd_extract)

    p = sub.add_parser("facts", help="inspect extracted facts")
    p.add_argument("doc_id", type=int, nargs="?")
    p.add_argument("--query")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=_cmd_facts)

    p = sub.add_parser("review", help="inspect rejected extractions")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(fn=_cmd_review)

    p = sub.add_parser("link", help="consolidate the attribute vocabulary")
    p.add_argument("--no-llm", action="store_true", help="fuzzy grouping only")
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--max-calls", type=int, default=0)
    p.set_defaults(fn=_cmd_link)

    p = sub.add_parser("reconcile", help="find corroborations and contradictions")
    p.add_argument("--no-llm", action="store_true", help="rule-decided relations only")
    p.add_argument("--max-adjudications", type=int, default=250)
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--max-calls", type=int, default=0)
    p.set_defaults(fn=_cmd_reconcile)

    p = sub.add_parser("relations", help="inspect discovered relationships")
    p.add_argument("--type", help="CORROBORATES | CONTRADICTS | RECONCILED_BY_CONTEXT | ...")
    p.add_argument("--query", help="match subject, attribute, either value, or explanation")
    p.add_argument("--cross-document", action="store_true")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(fn=_cmd_relations)

    p = sub.add_parser("schema", help="the discovered attribute vocabulary")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(fn=_cmd_schema)

    p = sub.add_parser("evidence", help="render a fact's evidence on its page")
    p.add_argument("fact_id", type=int)
    p.set_defaults(fn=_cmd_evidence)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
