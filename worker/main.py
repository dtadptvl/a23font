"""Production worker skeleton for the A23Font reconstruction pipeline.

M2 production-reachability slice: the worker polls the job queue, claims the
oldest QUEUED job (limited to one active collection) and fails it honestly
with PIPELINE_NOT_IMPLEMENTED until the real pipeline lands (milestone M5).
Stale active jobs owned by other workers are observed but not touched yet.

Run headless forever: python -m worker.main
"""
from __future__ import annotations

import logging
import os
import platform
import signal
import sys
import time
from typing import List

from app import services
from app.config import Config
from app.db import open_db, utcnow_iso
from app.logging_conf import log_event, setup_logging

POLL_SECONDS = 5.0
HEARTBEAT_EVERY = 12  # heartbeat line every 12 idle iterations (~60s)
ACTIVE_STATUSES = (
    "RESOLVING",
    "DISCOVERING",
    "RECONSTRUCTING",
    "VALIDATING",
    "PACKAGING",
)

_shutdown = False


def _handle_signal(signum, frame) -> None:
    global _shutdown
    _shutdown = True


def worker_identity() -> str:
    """Stable-enough identity for claim/stale attribution."""
    host = platform.node() or "unknown"
    return f"worker-{host}-{os.getpid()}"


def poll_once(conn, self_id: str, cfg: Config, logger: logging.Logger) -> bool:
    """One queue pass. Returns True when a job was claimed this pass."""
    placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)

    # Stale-recovery observation (M2 slice): active jobs owned by this
    # worker id stay as-is; other workers' stale active jobs are NOT
    # touched until the pipeline milestone lands.
    active_rows = conn.execute(
        f"SELECT id, worker_id FROM jobs WHERE status IN ({placeholders})",
        ACTIVE_STATUSES,
    ).fetchall()
    own_active: List[str] = [r["id"] for r in active_rows if r["worker_id"] == self_id]
    other_active: List[str] = [r["id"] for r in active_rows if r["worker_id"] != self_id]
    if own_active or other_active:
        log_event(
            logger,
            "active_jobs_observed",
            own=len(own_active),
            other_workers=len(other_active),
            limit=cfg.max_active_collections,
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        active_count = conn.execute(
            f"SELECT COUNT(*) AS n FROM jobs WHERE status IN ({placeholders})",
            ACTIVE_STATUSES,
        ).fetchone()["n"]
        if active_count >= cfg.max_active_collections:
            conn.rollback()
            return False
        row = conn.execute(
            "SELECT id FROM jobs WHERE status = 'QUEUED'"
            " ORDER BY created_at ASC, rowid ASC LIMIT 1"
        ).fetchone()
        if row is None:
            conn.rollback()
            return False
        job_id = row["id"]
        now = utcnow_iso()
        cursor = conn.execute(
            "UPDATE jobs SET status = 'RESOLVING', worker_id = ?, started_at = ?,"
            " updated_at = ?, attempts = attempts + 1"
            " WHERE id = ? AND status = 'QUEUED'",
            (self_id, now, now, job_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    services.add_event(conn, job_id, "claimed", {"worker_id": self_id})
    log_event(logger, "job_claimed", job_id=job_id, worker_id=self_id)

    # Honest placeholder: reconstruction pipeline is not part of this build.
    services.finish_job(
        conn,
        job_id,
        "FAILED",
        error_code="PIPELINE_NOT_IMPLEMENTED",
        error_message="reconstruction pipeline not installed in this build",
    )
    log_event(logger, "job_failed_placeholder", job_id=job_id, error_code="PIPELINE_NOT_IMPLEMENTED")
    return True


def main() -> int:
    cfg = Config.from_env()
    dirs = cfg.ensure_dirs()
    setup_logging(cfg.log_level, logfile=dirs["logs"] / "worker.log")
    logger = logging.getLogger("worker")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    self_id = worker_identity()
    db_path = cfg.data_root / "db" / "a23font.db"
    log_event(logger, "worker_started", worker_id=self_id, db=str(db_path))

    idle = 0
    iteration = 0
    while not _shutdown:
        iteration += 1
        claimed = False
        conn = open_db(db_path)
        try:
            claimed = poll_once(conn, self_id, cfg, logger)
        except Exception:
            logger.exception("poll_failed")
        finally:
            conn.close()
        if claimed:
            idle = 0
        else:
            idle += 1
            if idle % HEARTBEAT_EVERY == 0:
                log_event(logger, "heartbeat", iteration=iteration, idle=idle)
        waited = 0.0
        while not _shutdown and waited < POLL_SECONDS:
            time.sleep(min(0.5, POLL_SECONDS - waited))
            waited += 0.5

    log_event(logger, "worker_stopped", worker_id=self_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
