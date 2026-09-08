"""SQLite storage.

One file, no server, no migration tooling. Relations are rows in a table rather than a graph
database: the assignment is explicit that a graph is not the contribution, and a table keeps
setup to a single `pip install`.

The full schema is declared here even though later milestones populate most of it, so the
shape of the knowledge layer is legible in one place.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ---------------------------------------------------------------- source documents
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT NOT NULL UNIQUE,       -- re-uploading a document is a no-op
    filename      TEXT NOT NULL,
    title         TEXT,
    publisher     TEXT,
    doc_type      TEXT,
    -- The document's own vintage. This is what separates "a later report revised an
    -- earlier estimate" from "two sources contradict each other".
    published_on  TEXT,
    as_of         TEXT,
    page_count    INTEGER,
    status        TEXT NOT NULL DEFAULT 'pending',
    meta_json     TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pages (
    doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no    INTEGER NOT NULL,             -- 0-based index into the PDF
    label      TEXT,                         -- page number printed on the page, if any
    text       TEXT NOT NULL,
    width      REAL,
    height     REAL,
    PRIMARY KEY (doc_id, page_no)
);

-- Units of extraction. A block is what the model sees, which is also what grounding
-- verifies against.
CREATE TABLE IF NOT EXISTS blocks (
    id            INTEGER PRIMARY KEY,
    doc_id        INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no       INTEGER NOT NULL,
    ordinal       INTEGER NOT NULL,
    kind          TEXT NOT NULL,              -- paragraph | table | heading
    text          TEXT NOT NULL,
    char_start    INTEGER,                    -- offset into pages.text (-1 for tables)
    char_end      INTEGER,
    bbox_json     TEXT,
    section_path  TEXT,                       -- e.g. "MD&A > Segment performance"
    context_json  TEXT NOT NULL DEFAULT '{}', -- footnotes and nearby qualifying text
    sha256        TEXT NOT NULL,              -- content address, for the extraction cache
    extractable   INTEGER NOT NULL DEFAULT 1  -- 0 = prefiltered, never sent to the model
);
CREATE INDEX IF NOT EXISTS idx_blocks_doc ON blocks(doc_id, page_no);
CREATE INDEX IF NOT EXISTS idx_blocks_sha ON blocks(sha256);

-- ---------------------------------------------------------------- the knowledge layer
-- The attribute vocabulary is data, not an enum: it grows as documents introduce new
-- kinds of fact. This is the "schema that evolves" requirement.
CREATE TABLE IF NOT EXISTS attributes (
    id           INTEGER PRIMARY KEY,
    canon_name   TEXT NOT NULL UNIQUE,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    value_kind   TEXT,
    unit_family  TEXT,
    n_facts      INTEGER NOT NULL DEFAULT 0,
    first_doc_id INTEGER REFERENCES documents(id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entities (
    id           INTEGER PRIMARY KEY,
    canon_name   TEXT NOT NULL UNIQUE,
    kind         TEXT,                        -- company | person | place | other
    aliases_json TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS facts (
    id             INTEGER PRIMARY KEY,
    doc_id         INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    block_id       INTEGER NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,

    subject        TEXT NOT NULL,
    subject_id     INTEGER REFERENCES entities(id),
    attribute_raw  TEXT NOT NULL,             -- as the model named it
    attribute_id   INTEGER REFERENCES attributes(id),

    value_kind     TEXT,
    value_num      REAL,                      -- normalised to the unit family's base unit
    value_unit     TEXT,
    unit_family    TEXT,
    value_text     TEXT,                      -- for non-numeric facts
    value_raw      TEXT NOT NULL,             -- exactly as written in the document
    sig_figs       INTEGER,

    -- Qualifiers are kept OUT of the attribute name on purpose: they are the components
    -- of the claim key, and separating them is what makes case 3 fall out of the model.
    period_label   TEXT,
    period_start   TEXT,
    period_end     TEXT,
    period_gran    TEXT,
    scope          TEXT,                      -- consolidated | standalone | segment name
    basis          TEXT,                      -- "excluding traded goods", "revised estimate"
    geography      TEXT,
    qualifiers_json TEXT NOT NULL DEFAULT '{}',

    claim_key      TEXT,                      -- the join key; see reason/rules.py

    -- evidence
    evidence_quote TEXT NOT NULL,
    evidence_page  INTEGER NOT NULL,
    char_start     INTEGER,
    char_end       INTEGER,
    bbox_json      TEXT,
    grounding      TEXT NOT NULL,             -- verbatim | fuzzy
    grounding_score REAL,

    confidence     REAL,
    extractor_version TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_facts_doc ON facts(doc_id);
CREATE INDEX IF NOT EXISTS idx_facts_claim ON facts(claim_key);
CREATE INDEX IF NOT EXISTS idx_facts_attr ON facts(attribute_id);

-- Facts that failed the grounding gate. Kept, not discarded: this is the review queue and
-- the honest answer to "show us an extraction failure".
CREATE TABLE IF NOT EXISTS rejected_facts (
    id          INTEGER PRIMARY KEY,
    doc_id      INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    block_id    INTEGER REFERENCES blocks(id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,               -- what the model proposed
    reason      TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS relations (
    id          INTEGER PRIMARY KEY,
    fact_a      INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    fact_b      INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,   -- CORROBORATES | CONTRADICTS | RECONCILED_BY_CONTEXT
                                 -- | SUPERSEDES | DERIVED_CONSISTENT
    discriminator TEXT,          -- which claim-key component differs: period | scope | ...
    explanation TEXT,
    confidence  REAL,
    decided_by  TEXT NOT NULL,   -- rule | llm  (every relation records who decided it)
    rule_label  TEXT,
    meta_json   TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (fact_a, fact_b, type)
);
CREATE INDEX IF NOT EXISTS idx_rel_type ON relations(type);
CREATE INDEX IF NOT EXISTS idx_rel_a ON relations(fact_a);
CREATE INDEX IF NOT EXISTS idx_rel_b ON relations(fact_b);

-- Content-addressed LLM cache. Keyed on block content + prompt version + model, so
-- re-ingesting a document, or adding a new one, never re-pays for unchanged work.
CREATE TABLE IF NOT EXISTS llm_cache (
    key         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    response    TEXT NOT NULL,
    model       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    doc_id      INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL DEFAULT 'queued',
    done        INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    message     TEXT,
    error       TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    subject, attribute_raw, value_raw, evidence_quote,
    content='facts', content_rowid='id'
);
"""

_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, subject, attribute_raw, value_raw, evidence_quote)
  VALUES (new.id, new.subject, new.attribute_raw, new.value_raw, new.evidence_quote);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, subject, attribute_raw, value_raw, evidence_quote)
  VALUES ('delete', old.id, old.subject, old.attribute_raw, old.value_raw, old.evidence_quote);
END;
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executescript(_FTS_TRIGGERS)
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def js(value: Any) -> str:
    """JSON-encode for a TEXT column."""
    return json.dumps(value, ensure_ascii=False, default=str)


# One rejection detail carries the offending digits so a single case can be diagnosed
# ("value digits '56' not present in the quoted evidence", from ground/verify.py). That
# is the right text on an individual row and the wrong text in a summary, where it
# splinters one failure mode into hundreds of one-line entries.
_DIGIT_DETAIL = re.compile(r"^value digits '.*' not present in the quoted evidence$")


def rejection_summary(conn: sqlite3.Connection) -> list[dict]:
    """Rejections grouped by what actually went wrong, commonest first.

    Grouped by reason *and* detail: "ungrounded" alone hides the distinction that
    matters, between a quote that was never in the document and a real quote carrying
    a number that is not in it. The second is the failure that looks exactly like a
    fact, and it is the one worth counting separately.
    """
    tally: dict[tuple[str, str], int] = {}
    for row in conn.execute(
        "SELECT reason, detail, COUNT(*) n FROM rejected_facts GROUP BY reason, detail"
    ):
        detail = (row["detail"] or "").strip()
        if _DIGIT_DETAIL.match(detail):
            detail = "value not present in the quoted evidence"
        key = (row["reason"], detail)
        tally[key] = tally.get(key, 0) + row["n"]
    return [
        {"reason": reason, "detail": detail, "n": n}
        for (reason, detail), n in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
