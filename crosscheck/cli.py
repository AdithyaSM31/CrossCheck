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
import sys
from pathlib import Path

from .config import settings
from .db import session
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
                    doc_id, limit=args.limit, progress=progress, client=client
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
        for r in conn.execute(
            "SELECT reason, COUNT(*) n FROM rejected_facts GROUP BY reason ORDER BY n DESC"
        ):
            print(f"  {r['n']:>5}  {r['reason']}")
        print("\n== sample rejections ==")
        for r in conn.execute(
            "SELECT * FROM rejected_facts ORDER BY id DESC LIMIT ?", (args.limit,)
        ):
            print(f"\n[{r['id']}] {r['reason']}: {r['detail']}")
            print(f"   {r['payload_json'][:300]}")
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

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
