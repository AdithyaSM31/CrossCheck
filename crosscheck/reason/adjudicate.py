"""Reconciliation: pair facts, decide how they relate, and record why.

Candidate pairs come from the *loose* key — same subject, same canonical attribute —
because that is the population where a reader might expect agreement. Rules settle every
pair they can. Only genuinely ambiguous ones reach the model, and every relation records
which decided it.

Pairing is done against a cluster of facts sharing the loose key rather than across the
whole corpus, so adding a document compares its facts against the clusters they join and
nothing else. That keeps ingest incremental instead of quadratic.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from ..db import js, session
from ..llm.client import LLMClient, LLMError
from . import rules
from .derived import find_all
from .facts import FactView, dedupe, load_facts
from .keys import differing_components, loose_key

MAX_CLUSTER_PAIRS = 120  # per cluster; guards against a pathological attribute
SYSTEM = """\
You adjudicate whether two facts extracted from documents agree.

You are given both facts with their verbatim evidence, their normalised values, the
documents they came from with publication dates, and which parts of their claims differ.

Return JSON:
{"label": "...", "discriminator": "...", "explanation": "...", "confidence": 0.0}

Labels:
- CORROBORATES        the same claim, and the values agree, possibly expressed differently.
- CONTRADICTS         the same claim, values genuinely incompatible, and nothing in the
                      evidence explains the difference.
- RECONCILED_BY_CONTEXT  the values differ, but something stated explains it -- a different
                      period, scope, basis, unit, definition, or data vintage.
- SUPERSEDES          the same claim, and B's document was published after A's and revises
                      A's figure: an estimate becoming an actual, a revision, a rebasing.
                      A is always the earlier-published fact and B the later one -- they
                      are given to you in that order. Never use this label the other way
                      around; if A is somehow the more authoritative or later figure, that
                      is not what this label means and you should use CONTRADICTS or
                      RECONCILED_BY_CONTEXT instead.
- UNRELATED           they are not the same measure after all.

"discriminator" names what explains the difference, when one does: period, scope, basis,
unit, vintage, definition. Otherwise "".

Rules:
1. Reason ONLY from the supplied evidence and document metadata. Do not use anything you
   know about these organisations or figures from elsewhere.
2. The explanation is one or two sentences and must refer to what each source actually
   says. Quote the deciding words where it helps.
3. If you cannot see a reason for the difference in the evidence, answer CONTRADICTS with
   lower confidence. Do not invent a reconciliation. A contradiction the reader can check is
   more useful than a reassuring guess.
4. Two figures that differ only by rounding corroborate.
"""


@dataclass
class ReconcileStats:
    facts: int = 0
    clusters: int = 0
    pairs: int = 0
    by_rule: int = 0
    by_llm: int = 0
    derived: int = 0
    skipped: int = 0
    labels: dict[str, int] = field(default_factory=dict)
    calls: int = 0

    def count(self, label: str) -> None:
        self.labels[label] = self.labels.get(label, 0) + 1

    def summary(self) -> str:
        counts = ", ".join(f"{k} {v}" for k, v in sorted(self.labels.items()))
        return (
            f"{self.facts} facts in {self.clusters} clusters, {self.pairs} pairs examined -> "
            f"{self.by_rule} by rule, {self.by_llm} by model, {self.derived} derived"
            + (f" [{counts}]" if counts else "")
        )


def _describe(f: FactView, tag: str) -> str:
    bits = [
        f"{tag} {f.subject} — {f.attribute} = {f.value_raw}",
        f"   normalised: {f.value_num} ({f.unit_family or 'unitless'})",
        f"   period: {f.period_label or 'not stated'}",
        f"   scope: {f.scope or 'not stated'} | basis: {f.basis or 'not stated'}",
        f"   source: {f.doc_title} ({f.publisher or 'unknown publisher'}), "
        f"published {f.published_on or 'unknown'}, page {f.evidence_page + 1}",
        f'   evidence: "{f.evidence_quote[:400]}"',
    ]
    return "\n".join(bits)


def build_clusters(facts: list[FactView]) -> dict[str, list[FactView]]:
    clusters: dict[str, list[FactView]] = {}
    for f in facts:
        clusters.setdefault(loose_key(f.subject, f.attribute), []).append(f)
    return {k: v for k, v in clusters.items() if len(v) > 1}


def candidate_pairs(cluster: list[FactView]) -> list[tuple[FactView, FactView]]:
    """Pairs worth adjudicating, cross-document first.

    A large cluster is capped rather than fully expanded: comparing a hundred restatements
    of the same figure adds nothing a dozen comparisons have not already shown.
    """
    pairs: list[tuple[FactView, FactView]] = []
    for i, a in enumerate(cluster):
        for b in cluster[i + 1 :]:
            pairs.append((a, b))
    pairs.sort(
        key=lambda ab: (
            ab[0].doc_id == ab[1].doc_id,  # cross-document first
            -min(ab[0].confidence, ab[1].confidence),
        )
    )
    return pairs[:MAX_CLUSTER_PAIRS]


def _order_by_date(a: FactView, b: FactView) -> tuple[FactView, FactView]:
    """Earlier-published fact first. Undated facts sort after dated ones -- SUPERSEDES
    can only be claimed between two dated documents, so pushing undated facts last keeps
    the ordering meaningful rather than arbitrary."""
    da, db = a.published_on or "9999-99-99", b.published_on or "9999-99-99"
    return (a, b) if (da, a.id) <= (db, b.id) else (b, a)


async def _ask(client: LLMClient, a: FactView, b: FactView, verdict) -> dict | None:
    early, late = _order_by_date(a, b)
    diffs = differing_components(early.claim_key, late.claim_key) or ["nothing"]
    user = (
        f"{_describe(early, 'A (earlier-published).')}\n\n"
        f"{_describe(late, 'B (later-published, or same/unknown date as A).')}\n\n"
        f"Parts of the claim that differ: {', '.join(diffs)}.\n"
        f"Rule-based observation: {verdict.detail or verdict.rule}\n\n"
        "Adjudicate."
    )
    try:
        data = await client.complete_json(SYSTEM, user, max_tokens=700)
    except (LLMError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("label"):
        return None
    return data


def _store(conn, a: FactView, b: FactView, *, label, discriminator, explanation,
           confidence, decided_by, rule_label, meta=None) -> bool:
    if label not in {rules.CORROBORATES, rules.CONTRADICTS, rules.RECONCILED,
                     rules.SUPERSEDES, rules.DERIVED}:
        return False
    lo, hi = (a, b) if a.id < b.id else (b, a)
    # SUPERSEDES is directional: fact_a is chronologically earlier, fact_b later.
    # The caller is responsible for passing the pair in that order (see _order_by_date).
    if label == rules.SUPERSEDES:
        lo, hi = a, b
    try:
        conn.execute(
            """INSERT OR IGNORE INTO relations
               (fact_a, fact_b, type, discriminator, explanation, confidence,
                decided_by, rule_label, meta_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (lo.id, hi.id, label, discriminator, explanation, confidence,
             decided_by, rule_label, js(meta or {})),
        )
    except Exception:
        return False
    return True


async def reconcile(
    client: LLMClient | None = None, *, max_llm_calls: int = 250, progress=None
) -> ReconcileStats:
    stats = ReconcileStats()

    with session() as conn:
        facts = dedupe(
            load_facts(conn, "f.value_num IS NOT NULL OR f.value_text IS NOT NULL")
        )
    stats.facts = len(facts)

    clusters = build_clusters(facts)
    stats.clusters = len(clusters)

    pending: list[tuple[FactView, FactView, rules.Verdict]] = []

    with session() as conn:
        for cluster in clusters.values():
            for a, b in candidate_pairs(cluster):
                stats.pairs += 1
                verdict = rules.classify(a, b)
                if verdict.label:
                    if _store(
                        conn, a, b,
                        label=verdict.label,
                        discriminator=verdict.discriminator,
                        explanation="",  # filled by the narrator below
                        confidence=verdict.confidence,
                        decided_by="rule",
                        rule_label=verdict.rule,
                    ):
                        stats.by_rule += 1
                        stats.count(verdict.label)
                elif verdict.needs_llm:
                    pending.append((a, b, verdict))
                else:
                    stats.skipped += 1

        # ---- derived corroboration (arithmetic, no model) -------------------------
        for d in find_all(facts):
            meta = {
                "kind": d.kind,
                "computed": round(d.computed, 4),
                "stated": d.stated,
                "parts": [p.id for p in d.parts],
                "part_labels": [p.describe() for p in d.parts],
            }
            if _store(
                conn, d.target, d.parts[0],
                label=rules.DERIVED,
                discriminator=d.kind,
                explanation=d.explanation,
                confidence=max(0.6, 1.0 - d.error * 5),
                decided_by="rule",
                rule_label=f"derived:{d.kind}",
                meta=meta,
            ):
                stats.derived += 1
                stats.count(rules.DERIVED)

    # ---- the ambiguous remainder, adjudicated by the model ------------------------
    pending.sort(key=lambda t: -min(t[0].confidence, t[1].confidence))
    pending = pending[:max_llm_calls]

    if pending and client is not None:
        sem = asyncio.Semaphore(1)

        async def handle(a: FactView, b: FactView, verdict) -> None:
            data = await _ask(client, a, b, verdict)
            stats.calls += 1
            if not data:
                return
            label = str(data.get("label", "")).strip().upper()
            # The model was shown A as the earlier-published fact and B as the later one;
            # store the same pair in that order so a SUPERSEDES relation's direction in the
            # database matches what the model actually reasoned about, regardless of the
            # arbitrary order the two facts happened to be compared in.
            early, late = _order_by_date(a, b)
            async with sem:
                with session() as conn:
                    if _store(
                        conn, early, late,
                        label=label,
                        discriminator=str(data.get("discriminator", ""))[:60],
                        explanation=str(data.get("explanation", ""))[:1200],
                        confidence=float(data.get("confidence", 0.6) or 0.6),
                        decided_by="llm",
                        rule_label=verdict.rule,
                    ):
                        stats.by_llm += 1
                        stats.count(label)
            if progress:
                progress("adjudicating", stats.calls, len(pending))

        await asyncio.gather(*(handle(a, b, v) for a, b, v in pending))

    _narrate_rule_relations()
    return stats


def _narrate_rule_relations() -> None:
    """Give rule-decided relations a plain-English explanation.

    Written from the recorded facts rather than by a model: the rule already knows exactly
    why it decided, so paying for prose would add cost and a chance to be wrong.
    """
    with session() as conn:
        rows = conn.execute(
            """SELECT r.id, r.type, r.discriminator, r.rule_label,
                      a.value_raw va, a.period_label pa, a.scope sa, a.basis ba,
                      COALESCE(da.publisher, da.filename) srca,
                      b.value_raw vb, b.period_label pb, b.scope sb, b.basis bb,
                      COALESCE(db.publisher, db.filename) srcb
                 FROM relations r
                 JOIN facts a ON a.id = r.fact_a
                 JOIN facts b ON b.id = r.fact_b
                 JOIN documents da ON da.id = a.doc_id
                 JOIN documents db ON db.id = b.doc_id
                WHERE r.decided_by = 'rule' AND (r.explanation IS NULL OR r.explanation = '')"""
        ).fetchall()

        updates = []
        for r in rows:
            if r["type"] == rules.CORROBORATES:
                # A discriminator is present when the two facts' full claim keys differ
                # (e.g. one reports consolidated, the other standalone) but the values
                # still happen to agree -- worth saying explicitly, since "they agree" on
                # its own reads as though the claims were identical in every respect.
                if r["discriminator"]:
                    text = (
                        f"{r['srca']} reports {r['va']} and {r['srcb']} reports {r['vb']}. "
                        f"These differ by {r['discriminator']}, but the values still agree."
                    )
                else:
                    text = (
                        f"{r['srca']} reports {r['va']} and {r['srcb']} reports {r['vb']} "
                        f"for the same claim; the values agree."
                    )
            elif r["type"] == rules.RECONCILED:
                dim = r["discriminator"] or "context"
                pairs = {
                    "period": (r["pa"], r["pb"]),
                    "scope": (r["sa"], r["sb"]),
                    "basis": (r["ba"], r["bb"]),
                }
                key = dim.split()[0]
                left, right = pairs.get(key, (None, None))
                detail = (
                    f" — {left or 'not stated'} against {right or 'not stated'}"
                    if key in pairs else ""
                )
                text = (
                    f"{r['srca']} reports {r['va']} and {r['srcb']} reports {r['vb']}. "
                    f"These are not the same claim: they differ by {dim}{detail}, "
                    f"which accounts for the difference."
                )
            else:
                continue
            updates.append((text, r["id"]))

        conn.executemany("UPDATE relations SET explanation = ? WHERE id = ?", updates)
