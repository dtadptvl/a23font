"""Job lifecycle services on top of the SQLite layer."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from .config import Config
from .db import utcnow_iso
from .ids import new_job_id
from .security import validate_source_url

JOB_STATUSES = [
    "QUEUED", "RESOLVING", "DISCOVERING", "RECONSTRUCTING", "VALIDATING",
    "PACKAGING", "DONE", "DONE_WITH_ERRORS", "FAILED", "CANCELLED",
]
TERMINAL = {"DONE", "DONE_WITH_ERRORS", "FAILED", "CANCELLED"}
_JOB_COLUMNS = frozenset({
    "source_url", "normalized_url", "options_json", "status", "stage",
    "error_code", "error_message", "collection_name", "styles_total",
    "styles_done", "styles_failed", "cancel_requested", "worker_id",
    "attempts", "zip_name", "zip_size", "report_json",
    "created_at", "updated_at", "started_at", "finished_at",
})


class QueueFullError(RuntimeError):
    """Raised when the job queue cannot accept another job."""


class NotFoundError(LookupError):
    """Raised when a job does not exist."""


class InvalidTransition(ValueError):
    """Raised on illegal status transitions."""


def create_job(conn: sqlite3.Connection, raw_url: str, vietnamese: bool, cfg: Config) -> Dict[str, Any]:
    """Validate the source URL and enqueue a new job."""
    normalized = validate_source_url(raw_url)
    placeholders = ", ".join("?" for _ in TERMINAL)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM jobs WHERE status NOT IN ({placeholders})",
        tuple(TERMINAL),
    ).fetchone()
    capacity = cfg.max_queue + cfg.max_active_collections
    if row["n"] >= capacity:
        raise QueueFullError(f"already {row['n']} active/queued jobs (capacity {capacity})")
    job_id = new_job_id()
    now = utcnow_iso()
    options = {"vietnamese": bool(vietnamese), "schema": cfg.pipeline_version}
    conn.execute(
        "INSERT INTO jobs (id, source_url, normalized_url, options_json, status,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, 'QUEUED', ?, ?)",
        (job_id, raw_url.strip(), normalized.url, json.dumps(options), now, now),
    )
    conn.commit()
    add_event(conn, job_id, "created", {"source_url": raw_url.strip(), "normalized_url": normalized.url})
    job = get_job(conn, job_id)
    if job is None:  # pragma: no cover - insert just succeeded
        raise NotFoundError(job_id)
    return job


def get_job(conn: sqlite3.Connection, job_id: str) -> Optional[Dict[str, Any]]:
    """Return the job row as a dict, or None when missing."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row is not None else None


def list_recent(conn: sqlite3.Connection, limit: int = 20) -> List[Dict[str, Any]]:
    """Return the most recent jobs, newest first."""
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def set_stage(conn: sqlite3.Connection, job_id: str, stage: str, **extra_fields: Any) -> None:
    """Update the pipeline stage (plus whitelisted extra columns)."""
    assignments: Dict[str, Any] = {"stage": stage, "updated_at": utcnow_iso()}
    for key, value in extra_fields.items():
        if key not in _JOB_COLUMNS:
            raise ValueError(f"unknown jobs column: {key}")
        assignments[key] = value
    columns = ", ".join(f"{key} = ?" for key in assignments)
    params = list(assignments.values()) + [job_id]
    cursor = conn.execute(f"UPDATE jobs SET {columns} WHERE id = ?", params)
    if cursor.rowcount == 0:
        raise NotFoundError(job_id)
    conn.commit()


def request_cancel(conn: sqlite3.Connection, job_id: str) -> bool:
    """Flag a non-terminal job for cancellation. Returns False when terminal."""
    job = get_job(conn, job_id)
    if job is None:
        raise NotFoundError(job_id)
    if job["status"] in TERMINAL:
        return False
    conn.execute(
        "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
        (utcnow_iso(), job_id),
    )
    conn.commit()
    add_event(conn, job_id, "cancel_requested", None)
    return True


def cancel_job(conn: sqlite3.Connection, job_id: str) -> bool:
    """Transition a non-terminal job to CANCELLED. Returns True when applied."""
    job = get_job(conn, job_id)
    if job is None:
        raise NotFoundError(job_id)
    if job["status"] in TERMINAL:
        return False
    now = utcnow_iso()
    conn.execute(
        "UPDATE jobs SET status = 'CANCELLED', cancel_requested = 1, updated_at = ?,"
        " finished_at = ? WHERE id = ?",
        (now, now, job_id),
    )
    conn.commit()
    add_event(conn, job_id, "cancelled", None)
    return True


def finish_job(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    zip_name: Optional[str] = None,
    report: Optional[Dict[str, Any]] = None,
) -> None:
    """Move a job into a terminal status with optional error/artifact data."""
    if status not in TERMINAL:
        raise InvalidTransition(f"finish status must be terminal, got {status}")
    job = get_job(conn, job_id)
    if job is None:
        raise NotFoundError(job_id)
    now = utcnow_iso()
    assignments: Dict[str, Any] = {"status": status, "updated_at": now, "finished_at": now}
    if error_code is not None:
        assignments["error_code"] = error_code
    if error_message is not None:
        assignments["error_message"] = error_message
    if zip_name is not None:
        assignments["zip_name"] = zip_name
    if report is not None:
        assignments["report_json"] = json.dumps(report)
    columns = ", ".join(f"{key} = ?" for key in assignments)
    params = list(assignments.values()) + [job_id]
    conn.execute(f"UPDATE jobs SET {columns} WHERE id = ?", params)
    conn.commit()
    add_event(conn, job_id, "finished", {"status": status})


def add_event(conn: sqlite3.Connection, job_id: str, event: str, detail: Optional[Dict[str, Any]] = None) -> None:
    """Append an audit event for a job."""
    detail_json = json.dumps(detail) if detail is not None else None
    conn.execute(
        "INSERT INTO job_events (job_id, ts, event, detail_json) VALUES (?, ?, ?, ?)",
        (job_id, utcnow_iso(), event, detail_json),
    )
    conn.commit()
