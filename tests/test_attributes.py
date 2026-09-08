"""Attribute vocabulary consolidation tests.

The interesting risk here is not the happy path — it is what happens when the model's
answer has to be matched back to the groups it was shown. Matching by index rather than by
display name is what these pin down.
"""

import pytest

from crosscheck.db import js, session
from crosscheck.link.attributes import _adjudicate, block_attributes, consolidate


class FakeClient:
    """A stand-in for LLMClient that returns a scripted response, spends nothing."""

    def __init__(self, response):
        self._response = response
        self.calls = 0

    async def complete_json(self, system, user, **kw):
        self.calls += 1
        return self._response


def _seed(conn, rows):
    """rows: list of (subject, attribute_raw, unit_family, value_raw)."""
    conn.execute(
        "INSERT INTO documents (sha256, filename, status) VALUES ('h', 'f.pdf', 'extracted')"
    )
    doc_id = conn.execute("SELECT id FROM documents").fetchone()["id"]
    conn.execute(
        "INSERT INTO blocks (doc_id, page_no, ordinal, kind, text, sha256)"
        " VALUES (?, 0, 0, 'paragraph', 'x', 'h')",
        (doc_id,),
    )
    block_id = conn.execute("SELECT id FROM blocks").fetchone()["id"]
    for subject, attr, unit, value in rows:
        conn.execute(
            """INSERT INTO facts
               (doc_id, block_id, subject, attribute_raw, value_raw, unit_family,
                claim_key, evidence_quote, evidence_page, grounding, confidence)
               VALUES (?,?,?,?,?,?,?,?,0,'verbatim',0.9)""",
            (doc_id, block_id, subject, attr, value, unit, f"k|{attr}|{unit}", value),
        )


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CROSSCHECK_DB", str(tmp_path / "t.db"))
    import importlib
    import crosscheck.config as config_mod
    importlib.reload(config_mod)
    import crosscheck.db as db_mod
    importlib.reload(db_mod)
    return db_mod


@pytest.mark.asyncio
async def test_index_based_matching_survives_duplicate_display_names(db):
    """Two groups can legitimately show the same text -- an attribute string recurring
    under two incompatible unit families is exactly what blocking must keep apart. If the
    model's answer were matched back by that display string, the two groups would collide
    in a lookup and one would silently vanish from consolidation."""
    with db.session() as conn:
        _seed(
            conn,
            [
                ("Delhivery", "growth", "percent", "12%"),
                ("India", "growth", "currency:INR", "Rs.100 Cr"),
            ],
        )

    # Both groups display as "growth" to the model; it must still be able to keep them
    # separate because it addresses them by index, not by the (colliding) name.
    fake = FakeClient({"groups": [
        {"canonical": "growth rate", "members": [0]},
        {"canonical": "growth amount", "members": [1]},
    ]})
    stats = await consolidate(fake, use_llm=True)

    with db.session() as conn:
        canons = {r["canon_name"] for r in conn.execute("SELECT canon_name FROM attributes")}
    assert canons == {"growth rate", "growth amount"}
    assert stats.new_canonical == 2


@pytest.mark.asyncio
async def test_facts_are_linked_by_raw_text_and_unit_family_together(db):
    """The same raw attribute text can legitimately carry two different units -- an
    extractor occasionally mislabels a growth percentage with the level's own attribute
    name. The final fact-linking step used to match on raw text alone, so whichever
    canonical group's UPDATE ran last would silently claim every fact sharing that text,
    including ones whose real unit belonged to a different, correctly-separated group."""
    with db.session() as conn:
        _seed(
            conn,
            [
                ("Delhivery", "revenue from services", "currency:INR", "Rs.8,142 Cr"),
                ("Delhivery", "revenue from services", "percent", "12.7%"),
            ],
        )

    fake = FakeClient({"groups": [
        {"canonical": "revenue from services", "members": [0]},
        {"canonical": "revenue from services growth", "members": [1]},
    ]})
    await consolidate(fake, use_llm=True)

    with db.session() as conn:
        rows = {
            r["unit_family"]: r["canon_name"]
            for r in conn.execute(
                """SELECT f.unit_family, a.canon_name FROM facts f
                   JOIN attributes a ON a.id = f.attribute_id"""
            )
        }
    assert rows == {
        "currency:INR": "revenue from services",
        "percent": "revenue from services growth",
    }


@pytest.mark.asyncio
async def test_a_group_the_model_forgets_to_place_is_not_lost(db):
    """If the model's response omits an index, that group keeps its own name rather than
    disappearing from the vocabulary."""
    with db.session() as conn:
        _seed(conn, [
            ("Delhivery", "ebitda", "currency:INR", "Rs.127 Cr"),
            ("Delhivery", "revenue", "currency:INR", "Rs.8,142 Cr"),
        ])

    fake = FakeClient({"groups": [{"canonical": "ebitda", "members": [0]}]})  # drops index 1
    await consolidate(fake, use_llm=True)

    with db.session() as conn:
        canons = {r["canon_name"] for r in conn.execute("SELECT canon_name FROM attributes")}
    assert "ebitda" in canons and "revenue" in canons


@pytest.mark.asyncio
async def test_out_of_range_index_is_dropped_not_crashed(db):
    with db.session() as conn:
        _seed(conn, [("Delhivery", "ebitda", "currency:INR", "Rs.127 Cr")])

    fake = FakeClient({"groups": [{"canonical": "ebitda", "members": [0, 5, -1]}]})
    stats = await consolidate(fake, use_llm=True)  # must not raise
    assert stats.new_canonical >= 1


@pytest.mark.asyncio
async def test_malformed_model_response_falls_back_to_original_names(db):
    with db.session() as conn:
        _seed(conn, [("Delhivery", "ebitda", "currency:INR", "Rs.127 Cr")])

    fake = FakeClient({"nonsense": True})
    stats = await consolidate(fake, use_llm=True)
    with db.session() as conn:
        assert conn.execute(
            "SELECT canon_name FROM attributes"
        ).fetchone()["canon_name"] == "ebitda"


@pytest.mark.parametrize(
    "level,ratio_or_other",
    [
        ("revenue from services", "capital expenditure as percentage of revenue from services"),
        ("credit growth", "credit to agriculture growth"),
        ("ebitda", "ebitda margin"),
        ("current account deficit", "current account balance"),
        ("total revenue", "total revenue from operations"),
    ],
)
def test_blocking_never_merges_a_phrase_with_its_own_superset(level, ratio_or_other):
    """Regression for the most consequential bug found this session. token_set_ratio
    scores a phrase and any string containing all its words as a perfect match, since it
    compares token *sets* -- a subset is fully "contained" in the superset regardless of
    what the extra words mean. That silently merged 'revenue from services' with a
    *percentage of* itself, and 'credit growth' with a different sector's credit growth,
    at a real run's scale of thousands of attributes. Worse, only one example per
    pre-blocked group ever reaches the LLM adjudication step, so a bad merge made here
    could never be split back apart downstream -- this has to be caught at blocking."""
    items = [
        {"attribute_raw": level, "unit_family": "percent", "n": 1, "example": "1"},
        {"attribute_raw": ratio_or_other, "unit_family": "percent", "n": 1, "example": "2"},
    ]
    groups = block_attributes(items, {})
    assert len(groups) == 2, f"{level!r} and {ratio_or_other!r} were merged"


def test_blocking_still_merges_genuine_wording_variants():
    """The fix must not overcorrect into merging nothing -- these are the same measure,
    stated differently, and should still collapse into one group for free."""
    items = [
        {"attribute_raw": "real gdp growth", "unit_family": "percent", "n": 1, "example": "1"},
        {"attribute_raw": "real gdp growth rate", "unit_family": "percent", "n": 1, "example": "2"},
        {"attribute_raw": "growth in real gdp", "unit_family": "percent", "n": 1, "example": "3"},
    ]
    groups = block_attributes(items, {})
    assert len(groups) == 1
    assert len(groups[0]["members"]) == 3


def test_known_vocabulary_seeds_blocking_without_llm():
    """A raw string matching an existing canonical alias should join it via fuzzy
    blocking alone, costing no model call."""
    items = [{"attribute_raw": "Revenue From Services", "unit_family": "currency:INR",
              "n": 1, "example": "Rs.100 Cr"}]
    vocab = {"revenue from services": {"aliases": ["revenue from services"],
                                        "unit_family": "currency:INR"}}
    groups = block_attributes(items, vocab)
    known = [g for g in groups if g["known"]]
    assert len(known) == 1 and known[0]["members"]


@pytest.mark.asyncio
async def test_colliding_canonical_names_across_units_are_split(db):
    """Naming is a per-batch decision, made with no visibility into any other batch's
    choices -- nothing stops the model from independently proposing the identical text
    "revenue from services" for a currency-valued level and, elsewhere, a percent-valued
    figure that was mislabeled with the same raw text. Left merged this is not just
    untidy: rules.classify() treats "number" and "percent" as comparable units on purpose
    (so a table cell that lost its column header can still be compared), so a canon
    spanning both would generate a RECONCILED_BY_CONTEXT relation claiming two unrelated
    measures are "the same claim, differing by unit" -- a wrong finding, not merely an
    imprecise schema entry."""
    with db.session() as conn:
        _seed(
            conn,
            [
                ("Delhivery", "revenue from services", "currency:INR", "Rs.8,142 Cr"),
                ("Delhivery", "revenue growth mislabelled", "percent", "12.7%"),
            ],
        )

    # The model assigns the SAME canonical text to both, despite them being different
    # measures -- exactly the collision that must be caught downstream of adjudication.
    fake = FakeClient({"groups": [
        {"canonical": "revenue from services", "members": [0]},
        {"canonical": "revenue from services", "members": [1]},
    ]})
    await consolidate(fake, use_llm=True)

    with db.session() as conn:
        rows = {
            r["unit_family"]: r["canon_name"]
            for r in conn.execute(
                """SELECT f.unit_family, a.canon_name FROM facts f
                   JOIN attributes a ON a.id = f.attribute_id"""
            )
        }
    assert len(set(rows.values())) == 2, f"units collided under one canonical name: {rows}"
    assert rows["currency:INR"] == "revenue from services"  # the majority/primary form
    assert "percent" in rows["percent"]  # disambiguated rather than silently merged


@pytest.mark.asyncio
async def test_adjudicate_uses_fallback_when_model_returns_no_members():
    fake = FakeClient({"groups": [{"canonical": "x"}]})  # no "members" key at all
    batch = [{"canon": "a", "unit_family": "", "members": [{"example": "1"}]}]
    out = await _adjudicate(fake, batch)
    assert out and out[0]["members"] == [0]  # falls back rather than losing the group
