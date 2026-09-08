"""The evolving attribute vocabulary.

Documents name the same measure differently — "real gdp", "real gdp growth", "growth in real
GDP at market prices". Unless those collapse to one canonical attribute, three publishers
reporting the same figure never get compared and the knowledge layer is just three disjoint
piles of facts.

The vocabulary is a table, never an enum. It starts empty and grows as documents introduce
measures nobody anticipated, which is what makes the system work on PDFs it has not seen.

Consolidation is two-stage to keep it cheap and incremental:

1. **Blocking, free.** Fuzzy string similarity groups obvious variants and pins each group
   to a unit family, so a percentage never merges with a currency amount.
2. **Adjudication, one call per batch.** The model names each group and splits any that
   should not have merged. The dangerous merge is a level with a rate of change — "revenue"
   with "revenue growth", "EBITDA" with "EBITDA margin" — because those look almost
   identical as strings and are entirely different claims.

Only attributes not already in the vocabulary are ever sent, so ingesting a new document
costs adjudication on its genuinely new measures and nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from rapidfuzz import fuzz

from ..db import js, session
from ..llm.client import LLMClient, LLMError
from ..reason.keys import claim_key, normalise_phrase
from ..normalize.periods import parse_period

MERGE_THRESHOLD = 85  # string similarity above which variants group without asking
BATCH = 40

SYSTEM = """\
You are consolidating a vocabulary of measured attributes pulled from financial and
statistical documents. You are given attribute names, each with the unit family its values
use and an example value.

Group names that denote THE SAME MEASURE, and give each group one canonical name.

The test for "same measure": once period, scope and basis are equal, could a value of one be
compared directly against a value of the other and disagreement mean something? If not, they
are different measures.

Each attribute is given with a numeric index. Return JSON:
{"groups": [{"canonical": "...", "members": [0, 3, 7]}]}
using those indices, not the attribute text, to say which inputs belong together.

Rules:
1. NEVER merge a level with a rate of change or a ratio. "revenue" and "revenue growth" are
   different measures. So are "EBITDA" and "EBITDA margin"; "GDP" and "GDP growth";
   "inflation" and "change in inflation". This is the most damaging mistake you can make
   here, because the names look nearly identical and the claims are unrelated.
2. NEVER merge a total with one of its components, or a whole with a segment.
3. NEVER merge measures with incompatible unit families.
4. Merge only wording differences: word order, plurals, filler words, abbreviations,
   spelled-out versus symbolic forms, and qualifiers that merely restate the measure.
5. The canonical name is a short lowercase noun phrase with no period, scope, currency or
   unit in it.
6. Every index must appear in exactly one group. A group may have a single member.
"""


@dataclass
class LinkStats:
    attributes_in: int = 0
    groups: int = 0
    new_canonical: int = 0
    facts_relinked: int = 0
    calls: int = 0

    def summary(self) -> str:
        return (
            f"{self.attributes_in} distinct attributes -> {self.groups} groups "
            f"({self.new_canonical} new canonical), {self.facts_relinked} facts relinked, "
            f"{self.calls} model calls"
        )


def _distinct_attributes(conn, only_unlinked: bool = True) -> list[dict]:
    sql = """SELECT attribute_raw, unit_family, COUNT(*) n,
                    MIN(value_raw) example
               FROM facts
              WHERE 1=1 {extra}
           GROUP BY attribute_raw, unit_family
           ORDER BY n DESC"""
    extra = "AND attribute_id IS NULL" if only_unlinked else ""
    return [dict(r) for r in conn.execute(sql.format(extra=extra))]


def _existing_vocabulary(conn) -> dict[str, dict]:
    out = {}
    for r in conn.execute("SELECT * FROM attributes"):
        row = dict(r)
        row["aliases"] = json.loads(row["aliases_json"] or "[]")
        out[row["canon_name"]] = row
    return out


def block_attributes(items: list[dict], vocabulary: dict[str, dict]) -> list[list[dict]]:
    """Greedy fuzzy grouping, gated on unit family.

    Attributes already in the vocabulary seed the groups, so a new document's "real gdp
    growth" attaches to the canonical name an earlier document established rather than
    starting a rival one.

    Uses ``token_sort_ratio``, never ``token_set_ratio``. The two look interchangeable and
    are not: token_set_ratio scores a phrase and its own superset as a perfect match, since
    it compares token *sets* and a subset is fully "contained" in the larger set. That
    silently merged "revenue from services" with "capital expenditure as percentage of
    revenue from services" and "credit growth" with "credit to agriculture growth" at a
    score of 100 -- a level merged with a ratio built from it, and a growth rate merged
    with a completely different sector's growth rate. Worse, blocking happens *before* the
    LLM ever sees these strings, and only one representative example per pre-merged group
    is shown to it, so a bad merge made here was never something the adjudication step
    could catch or split back apart. token_sort_ratio still normalises word order but
    correctly penalises the extra words, scoring every case above around 53-67 rather than
    100 -- comfortably below MERGE_THRESHOLD.
    """
    groups: list[dict] = []
    for canon, row in vocabulary.items():
        groups.append(
            {
                "canon": canon,
                "unit_family": row.get("unit_family") or "",
                "members": [],
                "known": True,
                "surface": [canon, *row["aliases"]],
            }
        )

    for item in items:
        name = normalise_phrase(item["attribute_raw"])
        unit = item["unit_family"] or ""
        best, best_score = None, 0.0
        for g in groups:
            # A percentage and a currency amount are never the same measure, whatever the
            # names look like.
            if g["unit_family"] and unit and g["unit_family"] != unit:
                continue
            score = max(
                fuzz.token_sort_ratio(name, normalise_phrase(s)) for s in g["surface"]
            )
            if score > best_score:
                best, best_score = g, score

        if best is not None and best_score >= MERGE_THRESHOLD:
            best["members"].append(item)
            best["surface"].append(item["attribute_raw"])
            if not best["unit_family"]:
                best["unit_family"] = unit
        else:
            groups.append(
                {
                    "canon": item["attribute_raw"],
                    "unit_family": unit,
                    "members": [item],
                    "known": False,
                    "surface": [item["attribute_raw"]],
                }
            )

    return [g for g in groups if g["members"]]


async def _adjudicate(client: LLMClient, batch: list[dict]) -> list[dict]:
    """Ask the model to name and, where necessary, split a batch of candidate groups.

    Referenced by index rather than by display name. Two unmergeable groups can
    legitimately show the same text -- an attribute string that recurs under two
    incompatible unit families is exactly the kind of thing blocking is supposed to keep
    apart -- and matching the model's answer back by string would either collide the two
    in a lookup dict or hand the model an ambiguous listing to begin with. An index has
    no such collision.
    """
    listing = "\n".join(
        f"{i}. {g['canon']} [unit: {g['unit_family'] or 'unknown'}; "
        f"example: {g['members'][0]['example']}]"
        for i, g in enumerate(batch)
    )
    fallback = [{"canonical": g["canon"], "members": [i]} for i, g in enumerate(batch)]
    try:
        data = await client.complete_json(
            SYSTEM, f"Attributes:\n{listing}", max_tokens=3000
        )
    except (LLMError, ValueError):
        return fallback

    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list) or not groups:
        return fallback

    out = []
    for g in groups:
        if not isinstance(g, dict) or not g.get("canonical"):
            continue
        indices = [
            m for m in (g.get("members") or [])
            if isinstance(m, int) and 0 <= m < len(batch)
        ]
        if indices:
            out.append({"canonical": g["canonical"], "members": indices})
    return out or fallback


async def consolidate(
    client: LLMClient | None = None, *, use_llm: bool = True
) -> LinkStats:
    """Consolidate unlinked attributes into the vocabulary and relink their facts."""
    stats = LinkStats()

    with session() as conn:
        items = _distinct_attributes(conn)
        vocabulary = _existing_vocabulary(conn)

    stats.attributes_in = len(items)
    if not items:
        return stats

    groups = block_attributes(items, vocabulary)
    fresh = [g for g in groups if not g["known"]]

    # canonical name -> the (raw attribute string, unit family) pairs that map to it.
    # Both fields together, never the raw string alone: the same raw text can legitimately
    # appear under more than one unit family (an extractor occasionally mislabels a growth
    # percentage with the level's own attribute name), and blocking already keeps those as
    # separate items for exactly that reason. Collapsing back to raw-string-only here would
    # throw that separation away right before the fact linking that is supposed to use it --
    # a fact whose real unit is "percent" would get attribute_id assigned by whichever
    # canonical group's raw-string match happened to run last, regardless of its own unit.
    mapping: dict[str, list[tuple[str, str]]] = {}
    for g in groups:
        if g["known"]:
            mapping.setdefault(g["canon"], []).extend(
                (m["attribute_raw"], m["unit_family"]) for m in g["members"]
            )

    if fresh and use_llm and client is not None:
        for i in range(0, len(fresh), BATCH):
            batch = fresh[i : i + BATCH]
            stats.calls += 1
            for decided in await _adjudicate(client, batch):
                canon = str(decided["canonical"]).strip().lower()[:120]
                for idx in decided.get("members") or []:
                    g = batch[idx]  # _adjudicate already validated the index range
                    mapping.setdefault(canon, []).extend(
                        (m["attribute_raw"], m["unit_family"]) for m in g["members"]
                    )
        # Anything the model failed to place keeps its own name rather than vanishing.
        placed = {pair for pairs in mapping.values() for pair in pairs}
        for g in fresh:
            missing = [
                (m["attribute_raw"], m["unit_family"])
                for m in g["members"]
                if (m["attribute_raw"], m["unit_family"]) not in placed
            ]
            if missing:
                mapping.setdefault(g["canon"], []).extend(missing)
    else:
        for g in fresh:
            mapping.setdefault(g["canon"], []).extend(
                (m["attribute_raw"], m["unit_family"]) for m in g["members"]
            )

    # A canonical name is a naming decision made per batch, with no visibility into what
    # any other batch chose -- so nothing stops the model from independently proposing the
    # identical text "revenue from services" for a currency-valued level in one batch and a
    # percent-valued, mislabeled growth figure in another. If left merged, this is not just
    # untidy: rules.classify() treats "number" and "percent" as comparable units (deliberately,
    # so a table cell that lost its column-header unit can still be compared), so a colliding
    # canon spanning both would generate a RECONCILED_BY_CONTEXT relation claiming two
    # unrelated measures are "the same claim, differing by unit" -- a wrong finding, not
    # merely an imprecise schema entry. Splitting any canon whose pairs span more than one
    # unit family, before anything is written, keeps the naming collision from becoming a
    # reconciliation error.
    for canon in list(mapping):
        pairs = mapping[canon]
        units = {u for _, u in pairs}
        if len(units) <= 1:
            continue
        del mapping[canon]
        counts = {u: sum(1 for _, x in pairs if x == u) for u in units}
        # Ties (equally represented units) break alphabetically rather than on set
        # iteration order, which Python does not guarantee -- deterministic output matters
        # here since it decides which unit keeps the bare canonical name.
        primary = min(counts, key=lambda u: (-counts[u], u))
        for u in units:
            key = canon if u == primary else f"{canon} ({u.split(':')[-1]})"
            mapping.setdefault(key, []).extend((r, x) for r, x in pairs if x == u)

    with session() as conn:
        for canon, pairs in mapping.items():
            pairs = sorted(set(pairs))
            if not pairs:
                continue
            raws = sorted({raw for raw, _ in pairs})

            row = conn.execute(
                "SELECT id, aliases_json FROM attributes WHERE canon_name = ?", (canon,)
            ).fetchone()
            if row:
                attr_id = row["id"]
                aliases = sorted(set(json.loads(row["aliases_json"] or "[]")) | set(raws))
                conn.execute(
                    "UPDATE attributes SET aliases_json = ? WHERE id = ?",
                    (js(aliases), attr_id),
                )
            else:
                stats.new_canonical += 1
                cur = conn.execute(
                    """INSERT INTO attributes (canon_name, aliases_json, unit_family)
                       VALUES (?,?,?)""",
                    (canon, js(raws), pairs[0][1] or None),
                )
                attr_id = int(cur.lastrowid)

            conn.executemany(
                "UPDATE facts SET attribute_id = ? WHERE attribute_raw = ? AND unit_family = ?",
                [(attr_id, raw, unit) for raw, unit in pairs],
            )

        conn.execute(
            """UPDATE attributes SET n_facts =
                 (SELECT COUNT(*) FROM facts WHERE facts.attribute_id = attributes.id)"""
        )
        stats.groups = len(mapping)
        stats.facts_relinked = rekey(conn)

    return stats


def rekey(conn) -> int:
    """Recompute claim keys against the canonical vocabulary.

    Keys are built at extraction time from the raw attribute string, which is right for a
    single document and wrong across a corpus. Once the vocabulary knows that "real gdp" and
    "real gdp growth" are one measure, the keys have to be rebuilt or nothing collides.
    """
    rows = conn.execute(
        """SELECT f.id, f.subject, f.scope, f.basis, f.unit_family,
                  f.period_label, f.attribute_raw, a.canon_name
             FROM facts f LEFT JOIN attributes a ON a.id = f.attribute_id"""
    ).fetchall()

    updates = []
    for r in rows:
        period = parse_period(r["period_label"]) if r["period_label"] else None
        updates.append(
            (
                claim_key(
                    subject=r["subject"],
                    attribute=r["canon_name"] or r["attribute_raw"],
                    period=period,
                    scope=r["scope"],
                    basis=r["basis"],
                    unit_family=r["unit_family"],
                ),
                r["id"],
            )
        )
    conn.executemany("UPDATE facts SET claim_key = ? WHERE id = ?", updates)
    return len(updates)
