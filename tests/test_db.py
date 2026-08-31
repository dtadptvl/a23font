"""Database layer tests."""
import sqlite3

from app.db import SCHEMA_VERSION, open_db


def test_open_db_creates_schema(tmp_data):
    conn = open_db(tmp_data / "db" / "a23font.db")
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"jobs", "style_jobs", "job_events", "meta"} <= tables
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert {"idx_jobs_status", "idx_style_jobs_job"} <= indexes
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()["value"]
        assert version == str(SCHEMA_VERSION)
    finally:
        conn.close()


def test_open_db_idempotent(tmp_data):
    db_path = tmp_data / "db" / "a23font.db"
    first = open_db(db_path)
    first.close()
    second = open_db(db_path)
    try:
        tables = {
            row["name"]
            for row in second.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"jobs", "style_jobs", "job_events", "meta"} <= tables
    finally:
        second.close()


def test_open_db_pragmas(tmp_data):
    conn = open_db(tmp_data / "db" / "a23font.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        conn.close()


def test_row_factory(tmp_data):
    conn = open_db(tmp_data / "db" / "a23font.db")
    try:
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()
