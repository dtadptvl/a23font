"""Job lifecycle services on top of the SQLite layer."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
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
ACTIVE_STAGES = ("RESOLVING", "DISCOVERING", "RECONSTRUCTING", "VALIDATING", "PACKAGING")
STYLE_ACTIVE = ("RECONSTRUCTING", "VALIDATING", "PACKAGING")
STYLE_TERMINAL_DONE = "DONE"
STYLE_TERMINAL_FAILED = "FAILED"
_JOB_COLUMNS = frozenset({
    "source_url", "normalized_url", "options_json", "status", "stage",
    "error_code", "error_message", "collection_name", "styles_total",
    "styles_done", "styles_failed", "cancel_requested", "worker_id",
    "attempts", "zip_name", "zip_size", "report_json",
    "created_at", "updated_at", "started_at", "finished_at",
    "worker_heartbeat",
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
    zip_size: Optional[int] = None,
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
    if zip_size is not None:
        assignments["zip_size"] = int(zip_size)
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


# ---------------------------------------------------------------------------
# M5: style rows, heartbeats, stale requeue, report persistence
# ---------------------------------------------------------------------------

def register_styles(
    conn: sqlite3.Connection,
    job_id: str,
    styles: List[Dict[str, Any]],
    max_styles: int = 0,
) -> List[Dict[str, Any]]:
    """Register style rows for a job (restart-safe / idempotent).

    Each style dict may carry name, md5, source_identity. When rows already
    exist for the job (worker restart after a claim) they are preserved, so
    style history is never lost or duplicated; styles_total is synced to the
    row count either way. Returns the style rows ordered by position.

    max_styles (A23FONT_MAX_STYLES): when > 0 only the FIRST max_styles style
    specs are registered; the truncation is recorded once as a
    "styles_truncated" audit event (note: "styles truncated").
    """
    job = get_job(conn, job_id)
    if job is None:
        raise NotFoundError(job_id)
    styles = list(styles or [])
    total_available = len(styles)
    limit = int(max_styles or 0)
    truncated = limit > 0 and total_available > limit
    if truncated:
        styles = styles[:limit]
    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM style_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()["n"]
    if existing == 0:
        for position, style in enumerate(styles, start=1):
            style = style or {}
            conn.execute(
                "INSERT INTO style_jobs (job_id, position, name, source_identity, md5, status)"
                " VALUES (?, ?, ?, ?, ?, 'QUEUED')",
                (
                    job_id,
                    position,
                    str(style.get("name") or f"Style {position}"),
                    style.get("source_identity"),
                    style.get("md5"),
                ),
            )
        if truncated:
            add_event(
                conn,
                job_id,
                "styles_truncated",
                {
                    "note": "styles truncated",
                    "available": total_available,
                    "kept": len(styles),
                    "max_styles": limit,
                },
            )
    conn.execute(
        "UPDATE jobs SET styles_total = (SELECT COUNT(*) FROM style_jobs WHERE job_id = ?),"
        " updated_at = ? WHERE id = ?",
        (job_id, utcnow_iso(), job_id),
    )
    conn.commit()
    rows = conn.execute(
        "SELECT * FROM style_jobs WHERE job_id = ? ORDER BY position ASC, id ASC", (job_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def set_style_status(
    conn: sqlite3.Connection,
    style_job_id: int,
    status: str,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    cache_hit: bool = False,
    duration_ms: Optional[int] = None,
    report_json: Optional[Any] = None,
) -> None:
    """Update one style row and recompute the parent job counters."""
    row = conn.execute(
        "SELECT * FROM style_jobs WHERE id = ?", (style_job_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(str(style_job_id))
    now = utcnow_iso()
    assignments: Dict[str, Any] = {"status": status, "cache_hit": 1 if cache_hit else 0}
    if error_code is not None:
        assignments["error_code"] = error_code
    if error_message is not None:
        assignments["error_message"] = error_message
    if duration_ms is not None:
        assignments["duration_ms"] = int(duration_ms)
    if report_json is not None:
        assignments["report_json"] = (
            report_json if isinstance(report_json, str) else json.dumps(report_json)
        )
    if status in STYLE_ACTIVE and row["started_at"] is None:
        assignments["started_at"] = now
    if status in (STYLE_TERMINAL_DONE, STYLE_TERMINAL_FAILED):
        assignments["finished_at"] = now
        if row["started_at"] is None:
            assignments["started_at"] = now
    columns = ", ".join(f"{key} = ?" for key in assignments)
    params = list(assignments.values()) + [style_job_id]
    conn.execute(f"UPDATE style_jobs SET {columns} WHERE id = ?", params)
    conn.execute(
        "UPDATE jobs SET"
        " styles_done = (SELECT COUNT(*) FROM style_jobs WHERE job_id = ? AND status = 'DONE'),"
        " styles_failed = (SELECT COUNT(*) FROM style_jobs WHERE job_id = ? AND status = 'FAILED'),"
        " updated_at = ? WHERE id = ?",
        (row["job_id"], row["job_id"], now, row["job_id"]),
    )
    conn.commit()


def touch_heartbeat(conn: sqlite3.Connection, job_id: str, worker_id: str) -> None:
    """Record worker liveness for a claimed job."""
    now = utcnow_iso()
    cursor = conn.execute(
        "UPDATE jobs SET worker_heartbeat = ?, worker_id = ?, updated_at = ? WHERE id = ?",
        (now, worker_id, now, job_id),
    )
    if cursor.rowcount == 0:
        raise NotFoundError(job_id)
    conn.commit()


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def requeue_stale_jobs(
    conn: sqlite3.Connection, worker_id: str, stale_after_s: int = 90
) -> int:
    """Requeue active jobs whose worker looks dead. Returns the count.

    Stale means: worker_heartbeat older than the cutoff, an unparseable
    heartbeat, or no heartbeat at all while owned by a different worker
    (pre-heartbeat claim from an older build / crashed claim). Jobs owned by
    the CURRENT worker without a heartbeat are left alone (fresh claim).
    Requeued jobs go back to QUEUED with stage NULL, attempts+1, worker
    cleared, and one "requeued_stale" audit event.
    """
    now_dt = datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(seconds=int(stale_after_s))
    placeholders = ", ".join("?" for _ in ACTIVE_STAGES)
    rows = conn.execute(
        f"SELECT id, worker_id, worker_heartbeat FROM jobs WHERE status IN ({placeholders})",
        ACTIVE_STAGES,
    ).fetchall()
    requeued = 0
    for row in rows:
        heartbeat = _parse_ts(row["worker_heartbeat"])
        if row["worker_heartbeat"] and heartbeat is None:
            stale = True  # unparseable heartbeat -> treat as stale
        elif heartbeat is not None:
            stale = heartbeat <= cutoff
        else:
            stale = row["worker_id"] != worker_id
        if not stale:
            continue
        now = utcnow_iso()
        conn.execute(
            "UPDATE jobs SET status = 'QUEUED', stage = NULL, worker_id = NULL,"
            " worker_heartbeat = NULL, attempts = attempts + 1, updated_at = ?"
            " WHERE id = ?",
            (now, row["id"]),
        )
        conn.commit()
        add_event(
            conn,
            row["id"],
            "requeued_stale",
            {"previous_worker": row["worker_id"], "requeued_by": worker_id},
        )
        requeued += 1
    return requeued


def set_job_report(conn: sqlite3.Connection, job_id: str, report: Dict[str, Any]) -> None:
    """Persist the job report JSON (resources, per-style summaries)."""
    cursor = conn.execute(
        "UPDATE jobs SET report_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(report), utcnow_iso(), job_id),
    )
    if cursor.rowcount == 0:
        raise NotFoundError(job_id)
    conn.commit()