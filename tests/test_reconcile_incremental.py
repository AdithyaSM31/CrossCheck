"""Reconcile must not re-pay for an adjudication it already made.

Every rule-decided relation is free and idempotent to re-derive, so re-scanning the whole
corpus for those on every call is harmless. An LLM adjudication is not free, and without a
check, uploading one more document would re-collect every already-decided ambiguous pair
from the ENTIRE corpus and pay the model for each of them again -- exactly the "rebuilding
all existing knowledge" the incremental-ingest design exists to avoid.
"""

import importlib

import pytest

from crosscheck.reason import rules


class FakeClient:
    """Counts calls and remembers which fact-id pairs it was asked about."""

    def __init__(self, label="CONTRADICTS"):
        self.calls = 0
        self.asked_pairs: list[tuple[int, int]] = []
        self._label = label

    async def complete_json(self, system, user, **kw):
        self.calls += 1
        return {
            "label": self._label, "discriminator": "", "confidence": 0.8,
            "explanation": "model reasoning",
        }


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CROSSCHECK_DB", str(tmp_path / "t.db"))
    import crosscheck.config as config_mod
    importlib.reload(config_mod)
    import crosscheck.db as db_mod
    importlib.reload(db_mod)
    return db_mod


_next_doc = iter(range(1, 100_000))


def _seed_ambiguous_pair(conn, *, published_a="2022-01-01", published_b="2025-01-01"):
    """Two facts sharing one claim key but disagreeing in value -- rules.classify()
    cannot settle this (it is "identical claim, values disagree"), so it always needs
    the model. Returns their fact ids."""
    ids = []
    for value, published in ((("9.2"), published_a), (("8.2"), published_b)):
        n = next(_next_doc)
        conn.execute(
            "INSERT INTO documents (sha256, filename, publisher, published_on, status)"
            " VALUES (?, 'f.pdf', ?, ?, 'extracted')",
            (f"h{n}", f"Publisher {n}", published),
        )
        doc_id = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        conn.execute(
            "INSERT INTO blocks (doc_id, page_no, ordinal, kind, text, sha256)"
            " VALUES (?, 0, 0, 'paragraph', 'x', 'h')",
            (doc_id,),
        )
        block_id = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        conn.execute(
            """INSERT INTO facts
               (doc_id, block_id, subject, attribute_raw, value_raw, value_num,
                unit_family, period_label, claim_key, evidence_quote, evidence_page,
                grounding, confidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,0,'verbatim',0.9)""",
            (doc_id, block_id, "India", "real gdp growth", value, float(value),
             "percent", "FY24", "india|real gdp growth|k|||percent", "q" * 20),
        )
        ids.append(conn.execute("SELECT last_insert_rowid() id").fetchone()["id"])
    return tuple(ids)


@pytest.mark.asyncio
async def test_second_reconcile_does_not_reask_an_already_decided_pair(db):
    from crosscheck.reason.adjudicate import reconcile

    with db.session() as conn:
        _seed_ambiguous_pair(conn)

    fake = FakeClient()
    await reconcile(fake, max_llm_calls=10)
    assert fake.calls == 1  # the one genuinely ambiguous pair was adjudicated

    await reconcile(fake, max_llm_calls=10)
    assert fake.calls == 1  # the second run must not spend on it again

    with db.session() as conn:
        n = conn.execute("SELECT COUNT(*) n FROM relations").fetchone()["n"]
    assert n == 1  # and no duplicate relation was written either


@pytest.mark.asyncio
async def test_supersedes_ordering_does_not_defeat_the_dedupe_check():
    """SUPERSEDES is stored in chronological order, not id order (see _store /
    _order_by_date), so a pair where the chronologically-earlier fact happens to have the
    HIGHER id is exactly the case that would slip past a naive id-sorted comparison."""
    import crosscheck.config as config_mod
    import crosscheck.db as db_mod
    import tempfile
    from pathlib import Path
    import os

    tmp = Path(tempfile.mkdtemp()) / "t.db"
    os.environ["CROSSCHECK_DB"] = str(tmp)
    importlib.reload(config_mod)
    importlib.reload(db_mod)
    from crosscheck.reason.adjudicate import reconcile

    with db_mod.session() as conn:
        # published_a is chronologically LATER despite being seeded (and therefore
        # id-assigned) FIRST, so fact_a's id ends up SMALLER than fact_b's id even though
        # fact_a is the chronologically LATER document.
        ids = _seed_ambiguous_pair(conn, published_a="2025-01-01", published_b="2022-01-01")

    fake = FakeClient(label="SUPERSEDES")
    await reconcile(fake, max_llm_calls=10)
    assert fake.calls == 1

    await reconcile(fake, max_llm_calls=10)
    assert fake.calls == 1, "SUPERSEDES's chronological storage order defeated the dedupe check"


@pytest.mark.asyncio
async def test_a_model_verdict_of_unrelated_is_still_deduped(db):
    """_store() used to silently drop any label outside its five storable types, so
    "UNRELATED" -- the model deciding two candidate-looking facts are not the same
    measure after all -- left no trace. The next reconcile() run had no way to tell that
    pair apart from one never asked about, and re-paid for it. On the real corpus this
    was the majority of adjudication cost: most candidate pairs turn out unrelated on
    inspection, and every one of them was being re-asked on every run."""
    from crosscheck.reason.adjudicate import reconcile

    with db.session() as conn:
        _seed_ambiguous_pair(conn)

    fake = FakeClient(label="UNRELATED")
    await reconcile(fake, max_llm_calls=10)
    assert fake.calls == 1

    await reconcile(fake, max_llm_calls=10)
    assert fake.calls == 1, "an UNRELATED verdict was not remembered, so it was re-asked"

    with db.session() as conn:
        stored = conn.execute(
            "SELECT type FROM relations WHERE decided_by = 'llm'"
        ).fetchone()
    assert stored["type"] == "UNRELATED"
