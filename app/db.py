"""SQLite persistence layer."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'QUEUED',
    stage TEXT,
    error_code TEXT,
    error_message TEXT,
    collection_name TEXT,
    styles_total INTEGER NOT NULL DEFAULT 0,
    styles_done INTEGER NOT NULL DEFAULT 0,
    styles_failed INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    zip_name TEXT,
    zip_size INTEGER,
    report_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS style_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    source_identity TEXT,
    md5 TEXT,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    error_code TEXT,
    error_message TEXT,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    report_json TEXT
);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    detail_json TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_style_jobs_job ON style_jobs(job_id, position);
"""


def utcnow_iso() -> str:
    """Return current UTC time as ISO-8601 text."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(path: Path) -> sqlite3.Connection:
    """Open (and if needed initialize) the SQLite database."""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn
