"""CrossCheck command line.

    python -m crosscheck.cli ingest starter-datasets/**/*.pdf
    python -m crosscheck.cli blocks <doc_id> --page 5
    python -m crosscheck.cli docs
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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
            """SELECT d.id, d.filename, d.page_count, d.status,
                      COUNT(b.id) blocks, COALESCE(SUM(b.extractable), 0) extractable
                 FROM documents d LEFT JOIN blocks b ON b.doc_id = d.id
                GROUP BY d.id ORDER BY d.id"""
        ).fetchall()
    if not rows:
        print("no documents ingested yet")
        return 0
    print(f"{'id':>3}  {'pages':>5}  {'blocks':>6}  {'extract':>7}  file")
    for r in rows:
        print(
            f"{r['id']:>3}  {r['page_count']:>5}  {r['blocks']:>6}  "
            f"{r['extractable']:>7}  {r['filename']}"
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
        print(f"\n--- block {r['id']} p{r['page_no']} {r['kind']}{flag} "
              f"[{r['section_path'] or '-'}]")
        text = r["text"]
        print(text if args.full else text[:600] + ("..." if len(text) > 600 else ""))
    print(f"\n{len(rows)} block(s)")
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

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
