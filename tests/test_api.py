"""API smoke tests against an isolated database.

Not exercising the LLM-backed pipeline (that needs a live key and real documents); these
confirm the HTTP surface is wired correctly against the schema — routes, joins, and the
shapes the UI depends on.
"""

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CROSSCHECK_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("CROSSCHECK_DATA_DIR", str(tmp_path / "data"))

    import crosscheck.config as config_mod
    importlib.reload(config_mod)
    import crosscheck.db as db_mod
    importlib.reload(db_mod)
    import crosscheck.api.app as app_mod
    importlib.reload(app_mod)

    with db_mod.session() as conn:
        conn.execute(
            "INSERT INTO documents (id, sha256, filename, title, publisher, published_on,"
            " page_count, status) VALUES (1, 'x', 'a.pdf', 'A Report', 'Acme', '2024-01-01', 10, 'extracted')"
        )
        conn.execute(
            "INSERT INTO blocks (id, doc_id, page_no, ordinal, kind, text, sha256)"
            " VALUES (1, 1, 0, 0, 'paragraph', 'Revenue was Rs.100 Cr in FY24.', 'h')"
        )
        conn.execute(
            """INSERT INTO facts
               (id, doc_id, block_id, subject, attribute_raw, value_kind, value_num,
                unit_family, value_raw, period_label, claim_key, evidence_quote,
                evidence_page, grounding, grounding_score, confidence)
               VALUES (1, 1, 1, 'Acme', 'revenue', 'money', 1e9, 'currency:INR', 'Rs.100 Cr',
                       'FY2023-24', 'acme|revenue|2023-04-01/2024-03-31||||currency:INR',
                       'Revenue was Rs.100 Cr in FY24.', 0, 'verbatim', 100.0, 0.9)"""
        )
        conn.execute(
            "INSERT INTO attributes (id, canon_name, aliases_json, unit_family, n_facts)"
            " VALUES (1, 'revenue', '[\"revenue\"]', 'currency:INR', 1)"
        )
        conn.execute(
            "INSERT INTO rejected_facts (doc_id, block_id, payload_json, reason, detail)"
            " VALUES (1, 1, '{}', 'ungrounded', 'quote not found')"
        )

    yield TestClient(app_mod.app)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200


def test_stats_reflects_seeded_data(client):
    r = client.get("/api/stats").json()
    assert r["documents"] == 1
    assert r["facts"] == 1
    assert r["rejected"] == 1


def test_documents_lists_the_seeded_document(client):
    r = client.get("/api/documents").json()
    assert len(r) == 1
    assert r[0]["title"] == "A Report"
    assert r[0]["facts"] == 1


def test_facts_returns_the_seeded_fact_with_evidence(client):
    r = client.get("/api/facts").json()
    assert r["total"] == 1
    f = r["facts"][0]
    assert f["value_raw"] == "Rs.100 Cr"
    assert f["evidence_quote"] == "Revenue was Rs.100 Cr in FY24."
    assert f["attribute"] == "revenue"  # canonical name, not the raw string


def test_facts_search_filters_by_query(client):
    assert client.get("/api/facts?q=revenue").json()["total"] == 1
    assert client.get("/api/facts?q=nonexistent").json()["total"] == 0


def test_facts_filters_by_document(client):
    assert client.get("/api/facts?doc=1").json()["total"] == 1
    assert client.get("/api/facts?doc=999").json()["total"] == 0


def test_fact_detail_includes_empty_relations(client):
    r = client.get("/api/facts/1").json()
    assert r["fact"]["value_raw"] == "Rs.100 Cr"
    assert r["relations"] == []


def test_fact_detail_404_for_missing_fact(client):
    assert client.get("/api/facts/999").status_code == 404


def test_relations_empty_before_reconciliation(client):
    r = client.get("/api/relations").json()
    assert r["relations"] == []
    assert r["counts"] == {}


def test_schema_lists_the_seeded_attribute(client):
    r = client.get("/api/schema").json()
    assert r["total"] == 1
    assert r["attributes"][0]["canon_name"] == "revenue"


def test_review_lists_the_seeded_rejection(client):
    r = client.get("/api/review").json()
    assert any(x["reason"] == "ungrounded" for x in r["reasons"])
    assert len(r["rejected"]) == 1


def test_upload_rejects_non_pdf(client):
    r = client.post(
        "/api/documents", files={"file": ("notes.txt", b"hello", "text/plain")}
    )
    assert r.status_code == 400


def test_job_404_for_unknown_id(client):
    assert client.get("/api/jobs/does-not-exist").status_code == 404


def test_evidence_image_404_for_unknown_fact(client):
    assert client.get("/api/facts/999/evidence.png").status_code == 404


def test_ui_is_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"CrossCheck" in r.content
