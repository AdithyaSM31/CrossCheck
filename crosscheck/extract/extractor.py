"""Fact extraction: model call, grounding gate, normalisation, persistence.

The pipeline per block is deliberately narrow:

    block -> cache? -> model -> validate shape -> GROUND -> normalise -> claim key -> store

Anything that fails validation or grounding lands in ``rejected_facts`` with a reason. That
table is not a log; it is the review queue the UI shows, and the honest answer to "show us an
extraction failure".

Caching is content-addressed on the block text plus the prompt version plus the model, so
re-ingesting a document, adding a new one, or resuming an interrupted run costs nothing for
work already done.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..config import EXTRACTOR_VERSION, PROMPT_VERSION, settings
from ..db import js, session
from ..ground.verify import ground
from ..llm.client import BudgetExceeded, LLMClient, LLMError, parse_json
from ..normalize.periods import Period, find_periods, parse_period
from ..normalize.values import ValueKind, parse_value
from ..reason.keys import claim_key
from .prompts import META_SYSTEM, SYSTEM, build_user_prompt

Progress = Callable[[str, int, int], None]

VALUE_KINDS = {"money", "percent", "count", "ratio", "date", "text", "entity"}


@dataclass
class ExtractStats:
    blocks: int = 0
    cached: int = 0
    proposed: int = 0
    accepted: int = 0
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    errors: int = 0

    def reject(self, reason: str) -> None:
        self.rejected += 1
        key = reason.split("'")[0].strip()[:60]
        self.reasons[key] = self.reasons.get(key, 0) + 1

    def summary(self) -> str:
        rate = self.accepted / self.proposed if self.proposed else 0.0
        top = sorted(self.reasons.items(), key=lambda kv: -kv[1])[:4]
        detail = "; ".join(f"{k} x{v}" for k, v in top)
        return (
            f"{self.blocks} blocks ({self.cached} cached) -> "
            f"{self.proposed} proposed, {self.accepted} grounded ({rate:.0%}), "
            f"{self.rejected} rejected"
            + (f" [{detail}]" if detail else "")
            + (f", {self.errors} block errors" if self.errors else "")
        )


def _cache_key(block_sha: str, model: str) -> str:
    return hashlib.sha256(
        f"{block_sha}|{PROMPT_VERSION}|{model}".encode()
    ).hexdigest()


# --------------------------------------------------------------------------- validation
def _clean_str(v: object, limit: int = 400) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in {"null", "none", "n/a", "unknown", ""}:
        return None
    return s[:limit]


# A wide table row is rendered to the model as "label: header=value; header=value; ...",
# and the extraction prompt is explicit that each cell is its own fact. Some models do not
# reliably follow that on rows with many columns, and instead copy the whole row back as
# one "value" string. That string still contains the real numbers, so it grounds perfectly
# -- the quote matches, and normalize.values.parse_value happily reads off the FIRST number
# it finds (typically a year, since row-major cells lead with a period like "2021/22=...")
# as if it were the fact's value. The result is a confidently wrong fact: a claimed
# "Cyclically adjusted balance" of 2021, sourced from real evidence, that passed every
# other check. Catching it here, before normalisation, is cheaper and more reliable than
# trying to repair it after the fact.
#
# The check is deliberately blunt rather than pattern-matched to a specific period shape:
# no legitimate single value in this corpus contains both '=' and ';' together -- every
# real value is a bare number, a signed or parenthesised amount, a currency string, a
# period label, or a short status word. Both characters appearing together is the
# signature of a serialised row, whatever the row's own key format happens to be.
def _looks_like_undecomposed_row(value: str) -> bool:
    return "=" in value and ";" in value


def validate(raw: object) -> tuple[dict | None, str]:
    """Coerce a model-proposed fact into our shape, or say why it cannot be."""
    if not isinstance(raw, dict):
        return None, "not an object"

    subject = _clean_str(raw.get("subject"), 200)
    attribute = _clean_str(raw.get("attribute"), 200)
    value = _clean_str(raw.get("value"), 300)
    quote = _clean_str(raw.get("evidence_quote"), 1200)

    if not attribute:
        return None, "missing attribute"
    if not value:
        return None, "missing value"
    if not quote:
        return None, "missing evidence quote"
    if _looks_like_undecomposed_row(value):
        return None, "value looks like a whole table row, not one cell"

    kind = (_clean_str(raw.get("value_kind")) or "").lower()
    if kind not in VALUE_KINDS:
        kind = ""

    try:
        confidence = float(raw.get("confidence", 0.7))
    except (TypeError, ValueError):
        confidence = 0.7

    return (
        {
            "subject": subject or "(unspecified)",
            "attribute": attribute.lower(),
            "value": value,
            "value_kind": kind,
            "period": _clean_str(raw.get("period"), 60),
            "scope": _clean_str(raw.get("scope"), 120),
            "basis": _clean_str(raw.get("basis"), 240),
            "geography": _clean_str(raw.get("geography"), 120),
            "evidence_quote": quote,
            "confidence": min(max(confidence, 0.0), 1.0),
        },
        "",
    )


def _resolve_period(stated: str | None, quote: str, block_text: str) -> Period | None:
    """Prefer the stated period; fall back to one written inside the quoted evidence.

    The fallback is limited to the quote on purpose. Taking a period from elsewhere in the
    block would attach a confident, wrong period to a value, which is worse than none.
    """
    if stated:
        p = parse_period(stated)
        if p:
            return p
    found = find_periods(quote)
    return found[0] if len(found) == 1 else None


# --------------------------------------------------------------------------- extraction
async def extract_block(
    client: LLMClient, block: dict, doc: dict, conn
) -> tuple[list[dict], list[dict], bool]:
    """Returns (accepted facts, rejections, was_cached)."""
    key = _cache_key(block["sha256"], client.cfg.model)
    row = conn.execute("SELECT response FROM llm_cache WHERE key = ?", (key,)).fetchone()

    cached = row is not None
    if cached:
        payload = json.loads(row["response"])
    else:
        prompt = build_user_prompt(
            text=block["prompt_text"],
            title=doc.get("title") or doc.get("filename", ""),
            publisher=doc.get("publisher") or "",
            page=str(block.get("page_label") or block["page_no"] + 1),
            provenance=(
                f"Published: {doc['published_on']}" if doc.get("published_on") else ""
            ),
        )
        resp = await client.complete(SYSTEM, prompt, max_tokens=8000)
        payload = parse_json(resp.text)
        conn.execute(
            "INSERT OR REPLACE INTO llm_cache (key, kind, response, model) VALUES (?,?,?,?)",
            (key, "extract", json.dumps(payload), client.cfg.model),
        )

    if isinstance(payload, list):
        proposed = payload
    elif isinstance(payload, dict):
        proposed = payload.get("facts") or []
    else:
        proposed = []
    if not isinstance(proposed, list):
        proposed = []

    accepted: list[dict] = []
    rejected: list[dict] = []
    source = block["prompt_text"]

    for raw in proposed:
        fact, why = validate(raw)
        if fact is None:
            rejected.append({"payload": raw, "reason": "invalid shape", "detail": why})
            continue

        g, why = ground(fact["evidence_quote"], fact["value"], source)
        if not g.ok:
            rejected.append({"payload": fact, "reason": "ungrounded", "detail": why})
            continue

        parsed = parse_value(fact["value"])
        period = _resolve_period(fact["period"], fact["evidence_quote"], source)

        fact.update(
            {
                "grounding": g.status,
                "grounding_score": g.score,
                "char_start": g.start,
                "char_end": g.end,
                "value_num": parsed.number,
                "value_unit": parsed.unit,
                "unit_family": parsed.unit_family,
                "sig_figs": parsed.sig_figs,
                "parsed_kind": parsed.kind.value,
                "period_obj": period,
                "claim_key": claim_key(
                    subject=fact["subject"],
                    attribute=fact["attribute"],
                    period=period,
                    scope=fact["scope"],
                    basis=fact["basis"],
                    unit_family=parsed.unit_family,
                ),
            }
        )
        accepted.append(fact)

    return accepted, rejected, cached


def _persist(conn, doc_id: int, block_id: int, page_no: int, accepted, rejected) -> None:
    for f in accepted:
        p: Period | None = f["period_obj"]
        conn.execute(
            """INSERT INTO facts
               (doc_id, block_id, subject, attribute_raw, value_kind, value_num,
                value_unit, unit_family, value_text, value_raw, sig_figs,
                period_label, period_start, period_end, period_gran,
                scope, basis, geography, qualifiers_json, claim_key,
                evidence_quote, evidence_page, char_start, char_end,
                grounding, grounding_score, confidence, extractor_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                doc_id, block_id, f["subject"], f["attribute"],
                f["value_kind"] or f["parsed_kind"], f["value_num"],
                f["value_unit"], f["unit_family"],
                f["value"] if f["value_num"] is None else None,
                f["value"], f["sig_figs"],
                p.label if p else None,
                p.start.isoformat() if p else None,
                p.end.isoformat() if p else None,
                p.granularity.value if p else None,
                f["scope"], f["basis"], f["geography"],
                js({"period_raw": f["period"], "period_ambiguous": bool(p and p.ambiguous)}),
                f["claim_key"],
                f["evidence_quote"], page_no, f["char_start"], f["char_end"],
                f["grounding"], f["grounding_score"], f["confidence"], EXTRACTOR_VERSION,
            ),
        )
    for r in rejected:
        conn.execute(
            """INSERT INTO rejected_facts (doc_id, block_id, payload_json, reason, detail)
               VALUES (?,?,?,?,?)""",
            (doc_id, block_id, js(r["payload"]), r["reason"], r.get("detail", "")),
        )


async def extract_document(
    doc_id: int,
    *,
    limit: int = 0,
    progress: Progress | None = None,
    client: LLMClient | None = None,
) -> ExtractStats:
    """Extract facts for one ingested document."""
    stats = ExtractStats()

    with session() as conn:
        doc = dict(
            conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        )
        rows = conn.execute(
            """SELECT id, page_no, kind, text, section_path, context_json, sha256
                 FROM blocks WHERE doc_id = ? AND extractable = 1 ORDER BY ordinal""",
            (doc_id,),
        ).fetchall()

    blocks = []
    for r in rows:
        ctx = json.loads(r["context_json"] or "{}")
        parts = []
        if r["section_path"]:
            parts.append(f"[Section: {r['section_path']}]")
        parts.append(r["text"])
        for note in ctx.get("footnotes", [])[:6]:
            parts.append(f"[Footnote on same page: {note}]")
        blocks.append(
            {
                "id": r["id"], "page_no": r["page_no"], "sha256": r["sha256"],
                "prompt_text": "\n".join(parts), "page_label": ctx.get("page_label"),
            }
        )
    if limit:
        blocks = blocks[:limit]

    owns_client = client is None
    if owns_client:
        client = LLMClient(
            settings.extract,
            concurrency=settings.concurrency,
            timeout=settings.request_timeout,
            max_calls=settings.max_calls,
            extra_body=settings.extract.extra_body(),
            tokens_per_minute=settings.tokens_per_minute,
        )

    done = 0
    lock = asyncio.Lock()

    async def run(block: dict) -> None:
        nonlocal done
        # One connection per task keeps SQLite writes short and serialised by the lock.
        try:
            with session() as conn:
                accepted, rejected, was_cached = await extract_block(
                    client, block, doc, conn
                )
                async with lock:
                    _persist(conn, doc_id, block["id"], block["page_no"], accepted, rejected)
        except BudgetExceeded:
            raise
        except (LLMError, ValueError, KeyError) as exc:
            stats.errors += 1
            with session() as conn:
                conn.execute(
                    """INSERT INTO rejected_facts
                       (doc_id, block_id, payload_json, reason, detail)
                       VALUES (?,?,?,?,?)""",
                    (doc_id, block["id"], js({}), "block failed", str(exc)[:400]),
                )
            return

        stats.blocks += 1
        stats.cached += int(was_cached)
        stats.proposed += len(accepted) + len(rejected)
        stats.accepted += len(accepted)
        for r in rejected:
            stats.reject(r.get("detail") or r["reason"])
        done += 1
        if progress:
            progress("extracting", done, len(blocks))

    try:
        await asyncio.gather(*(run(b) for b in blocks))
    except BudgetExceeded:
        pass
    finally:
        if owns_client:
            await client.aclose()

    with session() as conn:
        conn.execute("UPDATE documents SET status = 'extracted' WHERE id = ?", (doc_id,))
    return stats


# --------------------------------------------------------------------------- metadata
async def infer_metadata(doc_id: int, client: LLMClient) -> dict:
    """Identify the document from its opening pages.

    The publication date is the point: it is what later separates "a newer report revised an
    older estimate" from "two sources contradict each other".
    """
    with session() as conn:
        rows = conn.execute(
            "SELECT text FROM pages WHERE doc_id = ? ORDER BY page_no LIMIT 3", (doc_id,)
        ).fetchall()
        filename = conn.execute(
            "SELECT filename FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()["filename"]

    head = "\n\n".join(r["text"] for r in rows)[:6000]
    try:
        data = await client.complete_json(
            META_SYSTEM, f"Filename: {filename}\n\n--- OPENING PAGES ---\n{head}"
        )
    except (LLMError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}

    with session() as conn:
        conn.execute(
            """UPDATE documents SET title = ?, publisher = ?, doc_type = ?,
                      published_on = ?, as_of = ? WHERE id = ?""",
            (
                _clean_str(data.get("title"), 300),
                _clean_str(data.get("publisher"), 200),
                _clean_str(data.get("doc_type"), 60),
                _clean_str(data.get("published_on"), 20),
                _clean_str(data.get("as_of"), 20),
                doc_id,
            ),
        )
    return data
