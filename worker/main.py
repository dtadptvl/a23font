"""Production worker for the A23Font reconstruction pipeline (M5).

Lifecycle:
  startup  -> requeue stale active jobs from dead workers (heartbeat based)
  loop 5s  -> claim the oldest QUEUED job (max_active_collections=1),
              run it stage by stage with heartbeats, persist per-style rows,
              record resource observations, finish with honest semantics:
                all styles ok  -> DONE
                some failed    -> DONE_WITH_ERRORS
                all failed     -> FAILED
                cancel honored -> CANCELLED
  SIGTERM  -> graceful: stop after the current glyph checkpoint, write
              "worker_shutdown_graceful", leave the job in its active stage;
              the next startup requeues it (resume via cache checkpoints).

Network honesty: the default production runner (resolve + discovery + raster
over HTTP) is guarded by cfg.pipeline_live (A23FONT_PIPELINE_LIVE, default
false in this milestone). Off-A23 builds fail claimed jobs honestly with
NETWORK_DISABLED; T-007 enables and proves the live route on the real A23.
Tests inject a job_runner instead.

Run headless forever: python -m worker.main
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import platform
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from app import services
from app.config import Config
from app.db import open_db, utcnow_iso
from app.logging_conf import log_event, setup_logging
from pipeline.orchestrator import CancelledWork
from worker.live_http import LiveHttpError, RestrictedClient, live_raster_hosts

POLL_SECONDS = 5.0
HEARTBEAT_EVERY = 12  # heartbeat log line every 12 idle iterations (~60s)
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
    """Short unique worker id for claim/heartbeat attribution."""
    return "w-" + uuid.uuid4().hex[:10]


class JobError(RuntimeError):
    """Honest job-level failure with a machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class WorkerShutdownRequested(RuntimeError):
    """Graceful shutdown: leave the job resumable in its active stage."""


# ---------------------------------------------------------------------------
# resource observations (M5.A3)
# ---------------------------------------------------------------------------

def _max_rss_kb_posix() -> Optional[int]:
    """Peak RSS via resource.getrusage; ru_maxrss is bytes on Windows-family
    platforms and KB on Linux. Returns None when the module is unavailable."""
    try:
        import resource
    except ImportError:
        return None
    try:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ValueError, OSError, AttributeError):
        return None
    if sys.platform == "win32":
        return int(peak) // 1024  # bytes -> KB
    return int(peak)  # Linux reports KB


def _max_rss_kb_windows() -> Optional[int]:
    """Peak working set (RSS equivalent) via GetProcessMemoryInfo, in KB."""

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ProcessMemoryCounters),
            ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        handle = kernel32.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    except (AttributeError, OSError, ValueError):
        return None
    if not ok:
        return None
    return int(counters.PeakWorkingSetSize) // 1024  # bytes -> KB


def _max_rss_kb() -> Optional[int]:
    """Peak RSS in KB, platform-normalized (M5.A3)."""
    value = _max_rss_kb_posix()
    if value is None and sys.platform == "win32":
        value = _max_rss_kb_windows()
    return value


def _mem_available_kb() -> Optional[int]:
    """MemAvailable from /proc/meminfo when present (Linux); else None."""
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    return None
    return None


def _resources_report(conn, job_id: str, worker_id: str, started_monotonic: float) -> Dict[str, Any]:
    rows = conn.execute(
        "SELECT name, status, duration_ms, cache_hit FROM style_jobs"
        " WHERE job_id = ? ORDER BY position ASC, id ASC",
        (job_id,),
    ).fetchall()
    return {
        "worker_id": worker_id,
        "duration_s": round(time.monotonic() - started_monotonic, 3),
        "max_rss_kb": _max_rss_kb(),
        "mem_available_kb": _mem_available_kb(),
        "platform": platform.system(),
        "rss_note": "resource.getrusage ru_maxrss normalized to KB (bytes on Windows)",
        "styles": [
            {
                "name": row["name"],
                "status": row["status"],
                "duration_ms": row["duration_ms"],
                "cache_hit": bool(row["cache_hit"]),
            }
            for row in rows
        ],
    }


# ---------------------------------------------------------------------------
# worker job context handed to runners
# ---------------------------------------------------------------------------

@dataclass
class WorkerJobCtx:
    conn: Any
    cfg: Config
    job: Dict[str, Any]
    worker_id: str
    logger: logging.Logger

    @property
    def job_id(self) -> str:
        return self.job["id"]

    def set_stage(self, stage: str) -> None:
        services.set_stage(self.conn, self.job_id, stage, worker_id=self.worker_id)
        services.touch_heartbeat(self.conn, self.job_id, self.worker_id)

    def heartbeat(self) -> None:
        services.touch_heartbeat(self.conn, self.job_id, self.worker_id)

    def cancel_requested(self) -> bool:
        row = services.get_job(self.conn, self.job_id)
        return bool(row and row.get("cancel_requested"))

    def check_cancel(self) -> None:
        """Raise WorkerShutdownRequested / CancelledWork when either is set."""
        if _shutdown:
            raise WorkerShutdownRequested("worker shutdown requested")
        if self.cancel_requested():
            raise CancelledWork("job cancel requested")


# ---------------------------------------------------------------------------
# default production runner (network path, guarded by cfg.pipeline_live)
# ---------------------------------------------------------------------------

async def default_job_runner(wctx: WorkerJobCtx) -> None:
    """resolve -> register styles -> discover -> reconstruct per style.

    The live network route (myfonts resolve, gmap discovery, sig.monotype
    raster fetches) requires A23FONT_PIPELINE_LIVE=true; in this milestone
    the default is false and jobs fail honestly with NETWORK_DISABLED.
    """
    cfg = wctx.cfg
    conn = wctx.conn
    job = wctx.job
    if not getattr(cfg, "pipeline_live", False):
        raise JobError(
            "NETWORK_DISABLED",
            "live pipeline network disabled in this build "
            "(A23FONT_PIPELINE_LIVE=true enables it; T-007 proves it on the A23)",
        )

    from app import security
    from pipeline import discovery
    from pipeline.cache import CacheStore
    from pipeline.metrics import estimate_from_manifest
    from pipeline.orchestrator import OrchestratorCtx, reconstruct_style
    from pipeline.raster import RasterProvider
    from pipeline.source_myfonts import resolve as resolve_source

    options = json.loads(job.get("options_json") or "{}")
    vietnamese = bool(options.get("vietnamese"))

    wctx.set_stage("RESOLVING")
    try:
        request = await resolve_source(job["source_url"], cfg, vietnamese)
    except security.SourceError as exc:
        raise JobError(str(exc.code).upper(), str(exc)) from exc

    style_specs = [
        {
            "name": style.name,
            "md5": style.identity.value if style.identity.kind == "md5" else None,
            "source_identity": style.identity.stable_id,
        }
        for style in request.styles
    ]
    rows = services.register_styles(conn, wctx.job_id, style_specs)
    wctx.set_stage("DISCOVERING")

    cache = CacheStore(cfg.data_root / "cache" / "pipeline", cfg.pipeline_version)
    obs_dir = cfg.data_root / "cache" / "observations"
    budget_s = float(getattr(cfg, "execution_budget_minutes", 15)) * 60.0

    # Restricted live client: allowlist (sig.monotype.com + configured extra
    # source hosts), bounded retries for the flaky mobile network, IPv4-only
    # sockets, hop-by-hop redirect validation (see worker.live_http).
    hosts = live_raster_hosts(cfg)
    async with RestrictedClient(hosts) as live:

        async def fetch_page(url: str):
            try:
                return await live.get_json(url)
            except LiveHttpError as exc:
                raise JobError(exc.code, exc.message) from exc

        async def fetch_bytes(url: str) -> Optional[bytes]:
            try:
                return await live.get_bytes(url)
            except LiveHttpError as exc:
                raise JobError(exc.code, exc.message) from exc

        for style_ref, row in zip(request.styles, rows):
            wctx.check_cancel()
            services.set_style_status(conn, row["id"], "RECONSTRUCTING")
            t0 = time.monotonic()
            if style_ref.identity.kind != "md5":
                services.set_style_status(
                    conn,
                    row["id"],
                    "FAILED",
                    error_code="DISCOVERY_NO_MD5",
                    error_message="fallback identity has no md5 for gmap discovery",
                )
                continue
            md5 = style_ref.identity.value
            manifest = await discovery.discover_glyphs(fetch_page, md5)
            if manifest.total_glyphs == 0:
                services.set_style_status(
                    conn,
                    row["id"],
                    "FAILED",
                    error_code="DISCOVERY_EMPTY",
                    error_message=(
                        "glyph discovery produced zero glyphs"
                        f" (stop={manifest.stop_reason})"
                        f" {'; '.join(manifest.notes)[:300]}"
                    ),
                )
                continue
            metrics = estimate_from_manifest(manifest, {})
            raster = RasterProvider(cfg, fetch_bytes=fetch_bytes, cache_obs_dir=obs_dir)

            def cancel_check() -> bool:
                wctx.check_cancel()  # raises on shutdown/cancel
                return False

            def stage_cb(stage: str, detail: Dict[str, Any]) -> None:
                services.set_stage(conn, wctx.job_id, stage)
                services.touch_heartbeat(conn, wctx.job_id, wctx.worker_id)

            octx = OrchestratorCtx(
                cfg=cfg,
                cache=cache,
                raster=raster,
                cancel_check=cancel_check,
                stage_cb=stage_cb,
                budget_deadline=time.monotonic() + budget_s,
            )
            # The raster endpoint requires the raw 32-hex md5; the "md5:"-
            # prefixed stable_id 404s at sig.monotype.com (found live in
            # T-007). Cache identity is the raw md5 as well.
            res = await reconstruct_style(
                octx,
                md5,
                {"vietnamese": vietnamese},
                request.family_name,
                style_ref.name,
                manifest,
                metrics,
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            if res.ok:
                services.set_style_status(
                    conn,
                    row["id"],
                    "DONE",
                    cache_hit=bool(res.cache_hit),
                    duration_ms=duration_ms,
                    report_json={
                        "glyphs_frozen": res.glyphs_frozen,
                        "cache_hit": res.cache_hit,
                        "validation_passed": bool(res.validation.get("passed")),
                    },
                )
            else:
                services.set_style_status(
                    conn,
                    row["id"],
                    "FAILED",
                    error_code="STYLE_BUILD_FAILED",
                    error_message=res.error or "style reconstruction failed",
                    duration_ms=duration_ms,
                    report_json={"glyphs_frozen": res.glyphs_frozen, "error": res.error},
                )


# ---------------------------------------------------------------------------
# job execution framework
# ---------------------------------------------------------------------------

async def run_job(
    conn,
    cfg: Config,
    job: Dict[str, Any],
    worker_id: str,
    *,
    job_runner: Optional[Callable[[WorkerJobCtx], Awaitable[None]]] = None,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Run one claimed job to a terminal status. Returns the final status.

    WorkerShutdownRequested propagates to the caller (the job stays in its
    active stage and is requeued by the next startup).
    """
    logger = logger or logging.getLogger("worker")
    job_id = job["id"]
    started = time.monotonic()
    runner = job_runner if job_runner is not None else default_job_runner
    wctx = WorkerJobCtx(conn=conn, cfg=cfg, job=job, worker_id=worker_id, logger=logger)

    try:
        wctx.set_stage("RESOLVING")
        await runner(wctx)
        wctx.check_cancel()
    except WorkerShutdownRequested:
        services.add_event(conn, job_id, "worker_shutdown_graceful", {"worker_id": worker_id})
        log_event(logger, "job_left_resumable", job_id=job_id, worker_id=worker_id)
        raise
    except CancelledWork:
        report = _resources_report(conn, job_id, worker_id, started)
        services.set_job_report(conn, job_id, report)
        services.finish_job(conn, job_id, "CANCELLED", report=report)
        log_event(logger, "job_cancelled", job_id=job_id)
        return "CANCELLED"
    except JobError as exc:
        report = _resources_report(conn, job_id, worker_id, started)
        services.finish_job(
            conn, job_id, "FAILED",
            error_code=exc.code, error_message=exc.message, report=report,
        )
        log_event(logger, "job_failed", job_id=job_id, error_code=exc.code)
        return "FAILED"
    except Exception as exc:  # noqa: BLE001 - honest internal failure
        logger.exception("job_runner_failed")
        report = _resources_report(conn, job_id, worker_id, started)
        services.finish_job(
            conn, job_id, "FAILED",
            error_code="INTERNAL_ERROR",
            error_message=f"{type(exc).__name__}: {exc}"[:500],
            report=report,
        )
        return "FAILED"

    rows = conn.execute(
        "SELECT status FROM style_jobs WHERE job_id = ? ORDER BY position ASC, id ASC",
        (job_id,),
    ).fetchall()
    report = _resources_report(conn, job_id, worker_id, started)
    if not rows:
        services.finish_job(
            conn, job_id, "FAILED",
            error_code="NO_STYLES", error_message="source resolved zero styles",
            report=report,
        )
        return "FAILED"
    statuses = [row["status"] for row in rows]
    done = sum(1 for status in statuses if status == "DONE")
    if done == len(rows):
        services.finish_job(conn, job_id, "DONE", report=report)
        log_event(logger, "job_done", job_id=job_id, styles=len(rows))
        return "DONE"
    if done == 0:
        services.finish_job(
            conn, job_id, "FAILED",
            error_code="STYLES_FAILED",
            error_message=f"all {len(rows)} style(s) failed",
            report=report,
        )
        return "FAILED"
    services.finish_job(
        conn, job_id, "DONE_WITH_ERRORS",
        error_message=f"{len(rows) - done} of {len(rows)} styles failed",
        report=report,
    )
    log_event(logger, "job_done_with_errors", job_id=job_id, done=done, total=len(rows))
    return "DONE_WITH_ERRORS"


def claim_next_queued(conn, self_id: str, cfg: Config) -> Optional[str]:
    """Claim the oldest QUEUED job unless another job is already active.

    max_active_collections semantics: with the default of 1, a single active
    collection blocks further claims. Returns the claimed job id or None.
    """
    placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
    conn.execute("BEGIN IMMEDIATE")
    try:
        active_count = conn.execute(
            f"SELECT COUNT(*) AS n FROM jobs WHERE status IN ({placeholders})",
            ACTIVE_STATUSES,
        ).fetchone()["n"]
        if active_count >= cfg.max_active_collections:
            conn.rollback()
            return None
        row = conn.execute(
            "SELECT id FROM jobs WHERE status = 'QUEUED'"
            " ORDER BY created_at ASC, rowid ASC LIMIT 1"
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        job_id = row["id"]
        now = utcnow_iso()
        cursor = conn.execute(
            "UPDATE jobs SET status = 'RESOLVING', stage = 'RESOLVING', worker_id = ?,"
            " worker_heartbeat = ?, started_at = ?, updated_at = ?, attempts = attempts + 1"
            " WHERE id = ? AND status = 'QUEUED'",
            (self_id, now, now, now, job_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    services.add_event(conn, job_id, "claimed", {"worker_id": self_id})
    return job_id


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------

def main() -> int:
    cfg = Config.from_env()
    dirs = cfg.ensure_dirs()
    setup_logging(cfg.log_level, logfile=dirs["logs"] / "worker.log")
    logger = logging.getLogger("worker")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    self_id = worker_identity()
    db_path = cfg.data_root / "db" / "a23font.db"

    conn = open_db(db_path)
    try:
        requeued = services.requeue_stale_jobs(conn, self_id)
        log_event(logger, "startup_stale_requeue", requeued=requeued, worker_id=self_id)
    finally:
        conn.close()
    log_event(
        logger, "worker_started", worker_id=self_id, db=str(db_path),
        pipeline_live=bool(getattr(cfg, "pipeline_live", False)),
    )

    idle = 0
    iteration = 0
    while not _shutdown:
        iteration += 1
        claimed_id: Optional[str] = None
        conn = open_db(db_path)
        try:
            claimed_id = claim_next_queued(conn, self_id, cfg)
            if claimed_id is not None:
                job = services.get_job(conn, claimed_id)
                log_event(logger, "job_claimed", job_id=claimed_id, worker_id=self_id)
                try:
                    asyncio.run(run_job(conn, cfg, job, self_id, logger=logger))
                except WorkerShutdownRequested:
                    log_event(
                        logger, "worker_shutdown_graceful",
                        job_id=claimed_id, worker_id=self_id,
                    )
                except Exception:  # noqa: BLE001 - loop must survive
                    logger.exception("job_run_failed")
        except Exception:  # noqa: BLE001
            logger.exception("poll_failed")
        finally:
            conn.close()
        if _shutdown:
            break
        if claimed_id is not None:
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