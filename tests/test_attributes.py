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
async def test_adjudicate_uses_fallback_when_model_returns_no_members():
    fake = FakeClient({"groups": [{"canonical": "x"}]})  # no "members" key at all
    batch = [{"canon": "a", "unit_family": "", "members": [{"example": "1"}]}]
    out = await _adjudicate(fake, batch)
    assert out and out[0]["members"] == [0]  # falls back rather than losing the group
