"""document_path() must survive the database being opened on a different machine than the
one that ingested it.

This matters concretely: the sample database committed with this submission lets a reader
browse real, grounded facts and evidence with no API key, but its stored_path values are
absolute paths baked in on the machine that produced it. Without a fallback, evidence
rendering would be the one thing that only ever worked on that original machine.
"""

import importlib
import json

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CROSSCHECK_DB", str(tmp_path / "t.db"))
    import crosscheck.config as config_mod
    importlib.reload(config_mod)
    import crosscheck.db as db_mod
    importlib.reload(db_mod)
    import crosscheck.ground.render as render_mod
    importlib.reload(render_mod)
    return db_mod, render_mod


def _seed_document(db_mod, *, filename, stored_path):
    with db_mod.session() as conn:
        conn.execute(
            "INSERT INTO documents (sha256, filename, status, meta_json) VALUES (?,?,?,?)",
            ("h", filename, "extracted", json.dumps({"stored_path": stored_path})),
        )
        return conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]


def test_falls_back_to_starter_datasets_when_stored_path_is_gone(db):
    db_mod, render_mod = db
    doc_id = _seed_document(
        db_mod,
        filename="01-delhivery-prospectus-2022-excerpt.pdf",
        stored_path="C:/some/other/machine/uploads/fake.pdf",
    )
    path = render_mod.document_path(doc_id)
    assert path.exists()
    assert path.name == "01-delhivery-prospectus-2022-excerpt.pdf"
    assert "starter-datasets" in str(path)


def test_uses_the_stored_path_when_it_still_exists(db, tmp_path):
    db_mod, render_mod = db
    real = tmp_path / "real.pdf"
    real.write_bytes(b"%PDF-1.4 fake")
    doc_id = _seed_document(db_mod, filename="real.pdf", stored_path=str(real))
    assert render_mod.document_path(doc_id) == real


def test_raises_a_clear_error_when_nothing_matches(db):
    db_mod, render_mod = db
    doc_id = _seed_document(
        db_mod, filename="never-existed.pdf", stored_path="C:/gone/never-existed.pdf"
    )
    with pytest.raises(FileNotFoundError):
        render_mod.document_path(doc_id)


def test_raises_for_an_unknown_document_id(db):
    _, render_mod = db
    with pytest.raises(KeyError):
        render_mod.document_path(999)
