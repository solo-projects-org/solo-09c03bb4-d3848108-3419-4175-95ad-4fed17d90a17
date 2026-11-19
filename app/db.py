"""SQLite storage for the spectrum release console.

All writes that change the current source set, revoke a release or publish a
release use ``BEGIN IMMEDIATE`` so that an upload and the worker cannot
interleave partially.  Compute itself happens outside a write transaction.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('manifest', 'candidates')),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    content_sha256 TEXT NOT NULL,
    filename TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL,
    UNIQUE (kind, revision)
);

CREATE TABLE IF NOT EXISTS current_sources (
    kind TEXT PRIMARY KEY CHECK (kind IN ('manifest', 'candidates')),
    source_id INTEGER NOT NULL REFERENCES sources(id),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manifest_rows (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    sample TEXT NOT NULL,
    spectrum TEXT NOT NULL,
    PRIMARY KEY (source_id, ord)
);
CREATE INDEX IF NOT EXISTS idx_manifest_key
    ON manifest_rows (source_id, sample, spectrum);

CREATE TABLE IF NOT EXISTS candidate_rows (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    sample TEXT NOT NULL,
    spectrum TEXT NOT NULL,
    peptide TEXT NOT NULL,
    label TEXT NOT NULL CHECK (label IN ('target', 'decoy')),
    score TEXT NOT NULL,
    PRIMARY KEY (source_id, ord)
);
CREATE INDEX IF NOT EXISTS idx_candidate_key
    ON candidate_rows (source_id, sample, spectrum);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manifest_source_id INTEGER NOT NULL REFERENCES sources(id),
    candidate_source_id INTEGER NOT NULL REFERENCES sources(id),
    threshold TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'done', 'blocked', 'failed', 'superseded')
    ),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    summary_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, id);
CREATE INDEX IF NOT EXISTS idx_jobs_triple
    ON jobs (manifest_source_id, candidate_source_id, threshold, id);

CREATE TABLE IF NOT EXISTS job_issues (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    issue_type TEXT NOT NULL,
    title TEXT NOT NULL,
    count INTEGER NOT NULL,
    details_json TEXT NOT NULL,
    truncated INTEGER NOT NULL,
    PRIMARY KEY (job_id, idx)
);

CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    manifest_source_id INTEGER NOT NULL REFERENCES sources(id),
    candidate_source_id INTEGER NOT NULL REFERENCES sources(id),
    threshold TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_release_one_active
    ON releases (status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS release_rows (
    release_id INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    sample TEXT NOT NULL,
    spectrum TEXT NOT NULL,
    peptide TEXT NOT NULL,
    label TEXT NOT NULL,
    score TEXT NOT NULL,
    q_value TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    score_group INTEGER NOT NULL,
    PRIMARY KEY (release_id, ord)
);
CREATE INDEX IF NOT EXISTS idx_release_rows_accepted
    ON release_rows (release_id, accepted, rank);
"""

DEFAULT_THRESHOLD = "0.01"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def read_conn(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def init_db(db_path: str | Path, default_threshold: str = DEFAULT_THRESHOLD) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('fdr_threshold', ?)",
            (default_threshold,),
        )
    finally:
        conn.close()


def revoke_active_release(conn: sqlite3.Connection, reason: str) -> int:
    rows = conn.execute(
        "SELECT id FROM releases WHERE status = 'active'"
    ).fetchall()
    if not rows:
        return 0
    now = now_iso()
    conn.execute(
        "UPDATE releases SET status = 'revoked', revoked_at = ?, revoke_reason = ? "
        "WHERE status = 'active'",
        (now, reason),
    )
    return len(rows)


def supersede_open_jobs(conn: sqlite3.Connection, reason: str) -> int:
    rows = conn.execute(
        "SELECT id FROM jobs WHERE status IN ('pending', 'running')"
    ).fetchall()
    if not rows:
        return 0
    conn.execute(
        "UPDATE jobs SET status = 'superseded', finished_at = ?, "
        "error = COALESCE(error, ?) WHERE status IN ('pending', 'running')",
        (now_iso(), reason),
    )
    return len(rows)


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
