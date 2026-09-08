"""Tests for the plain-English narration written for rule-decided relations.

Written from the recorded facts rather than by a model, so these pin down exactly what a
reader sees on the Findings screen for the two rule-decided relation types.
"""

import importlib

import pytest

from crosscheck.reason import rules


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CROSSCHECK_DB", str(tmp_path / "t.db"))
    import crosscheck.config as config_mod
    importlib.reload(config_mod)
    import crosscheck.db as db_mod
    importlib.reload(db_mod)
    return db_mod


_next_doc = iter(range(1, 100_000))


def _seed_fact(conn, *, subject, value, period=None, scope=None, basis=None, doc_id=None):
    if doc_id is None:
        n = next(_next_doc)
        conn.execute(
            "INSERT INTO documents (sha256, filename, publisher, status)"
            " VALUES (?, 'f.pdf', ?, 'extracted')",
            (f"h{n}", f"Publisher {n}"),
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
           (doc_id, block_id, subject, attribute_raw, value_raw, period_label, scope,
            basis, claim_key, evidence_quote, evidence_page, grounding, confidence)
           VALUES (?,?,?,?,?,?,?,?,?,?,0,'verbatim',0.9)""",
        (doc_id, block_id, subject, "revenue", value, period, scope, basis, "k", "q" * 20),
    )
    return conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]


def test_corroborates_without_discriminator_reads_as_the_same_claim(db):
    from crosscheck.reason.adjudicate import _narrate_rule_relations

    with db.session() as conn:
        a = _seed_fact(conn, subject="Acme", value="Rs.100 Cr", period="FY24")
        b = _seed_fact(conn, subject="Acme", value="Rs.100 Cr", period="FY24")
        conn.execute(
            "INSERT INTO relations (fact_a, fact_b, type, discriminator, confidence,"
            " decided_by, rule_label) VALUES (?,?,?,?,?,?,?)",
            (a, b, rules.CORROBORATES, "", 0.95, "rule", "identical claim"),
        )
        conn.commit()
        _narrate_rule_relations()
        text = conn.execute(
            "SELECT explanation FROM relations WHERE fact_a=?", (a,)
        ).fetchone()["explanation"]
    assert "for the same claim" in text
    assert "differ" not in text


def test_corroborates_with_discriminator_names_what_still_differs(db):
    """The value agreeing despite a differing qualifier (e.g. consolidated vs standalone
    reporting the same figure) is worth saying explicitly -- a bare "they agree" would
    read as though the claims were identical in every respect, which they are not."""
    from crosscheck.reason.adjudicate import _narrate_rule_relations

    with db.session() as conn:
        a = _seed_fact(conn, subject="Acme", value="18,793", period="Q4 FY24",
                        scope="consolidated")
        b = _seed_fact(conn, subject="Acme", value="18,793", period="Q4 FY24",
                        scope="standalone")
        conn.execute(
            "INSERT INTO relations (fact_a, fact_b, type, discriminator, confidence,"
            " decided_by, rule_label) VALUES (?,?,?,?,?,?,?)",
            (a, b, rules.CORROBORATES, "scope", 0.75, "rule", "values agree despite scope"),
        )
        conn.commit()
        _narrate_rule_relations()
        text = conn.execute(
            "SELECT explanation FROM relations WHERE fact_a=?", (a,)
        ).fetchone()["explanation"]
    assert "differ by scope" in text
    assert "values still agree" in text


def test_reconciled_names_the_actual_periods(db):
    from crosscheck.reason.adjudicate import _narrate_rule_relations

    with db.session() as conn:
        a = _seed_fact(conn, subject="Acme", value="Rs.8,142 Cr", period="FY2023-24")
        b = _seed_fact(conn, subject="Acme", value="Rs.2,076 Cr", period="Q4 FY2023-24")
        conn.execute(
            "INSERT INTO relations (fact_a, fact_b, type, discriminator, confidence,"
            " decided_by, rule_label) VALUES (?,?,?,?,?,?,?)",
            (a, b, rules.RECONCILED, "period (one covers part of the other)", 0.9,
             "rule", "single differing qualifier: period"),
        )
        conn.commit()
        _narrate_rule_relations()
        text = conn.execute(
            "SELECT explanation FROM relations WHERE fact_a=?", (a,)
        ).fetchone()["explanation"]
    assert "FY2023-24" in text and "Q4 FY2023-24" in text
    assert "differ by period" in text


def test_narration_never_overwrites_an_existing_explanation(db):
    """decided_by='llm' relations already carry the model's own explanation; the
    narrator's WHERE clause must never touch those."""
    from crosscheck.reason.adjudicate import _narrate_rule_relations

    with db.session() as conn:
        a = _seed_fact(conn, subject="Acme", value="9.2", period="FY24")
        b = _seed_fact(conn, subject="Acme", value="8.2", period="FY24")
        conn.execute(
            "INSERT INTO relations (fact_a, fact_b, type, discriminator, explanation,"
            " confidence, decided_by, rule_label) VALUES (?,?,?,?,?,?,?,?)",
            (a, b, rules.CONTRADICTS, "", "the model's own reasoning", 0.8, "llm", ""),
        )
        conn.commit()
        _narrate_rule_relations()
        text = conn.execute(
            "SELECT explanation FROM relations WHERE fact_a=?", (a,)
        ).fetchone()["explanation"]
    assert text == "the model's own reasoning"


def test_narration_never_overwrites_a_derived_explanation(db):
    """DERIVED_CONSISTENT relations already carry the arithmetic explanation written at
    storage time; the narrator only fills in relations that were stored with none."""
    from crosscheck.reason.adjudicate import _narrate_rule_relations

    with db.session() as conn:
        a = _seed_fact(conn, subject="Acme", value="1.6%", period="FY24")
        b = _seed_fact(conn, subject="Acme", value="Rs.127 Cr", period="FY24")
        conn.execute(
            "INSERT INTO relations (fact_a, fact_b, type, discriminator, explanation,"
            " confidence, decided_by, rule_label) VALUES (?,?,?,?,?,?,?,?)",
            (a, b, rules.DERIVED, "ratio", "127 / 8142 = 1.56%, matching the stated 1.6%",
             0.9, "rule", "derived:ratio"),
        )
        conn.commit()
        _narrate_rule_relations()
        text = conn.execute(
            "SELECT explanation FROM relations WHERE fact_a=?", (a,)
        ).fetchone()["explanation"]
    assert text == "127 / 8142 = 1.56%, matching the stated 1.6%"
