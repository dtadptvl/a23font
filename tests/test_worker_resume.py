"""M5.A2 persistence/restart proofs + M5.A3 resource recording.

Scenario A (orchestrator crash/resume): an interrupt hook raises right after
the first glyph freeze; the cumulative checkpoint survives the crash (cache
lookup "partial"), and the resumed run completes without re-acquiring any
observation of the already-frozen glyph.

Scenario B (worker-level persistence): a real QUEUED job + registered style
runs through worker.run_job with an injected synthetic runner to DONE; the
jobs row, style_jobs rows, events and report_json resources (max_rss_kb,
duration_s, mem_available_kb) are verified. Then a restart is simulated: the
job is forced back into an active stage with a stale heartbeat,
requeue_stale_jobs requeues it (attempts+1, event), and the rerun finishes
DONE again with the style history preserved (no duplicate rows) and served
from the binary cache.

Cancel: a cancel requested between styles ends the job CANCELLED.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

from app import services
from app.config import Config
from app.db import open_db
from pipeline.cache import CacheStore
from pipeline.metrics import estimate_from_manifest
from pipeline.orchestrator import CancelledWork, OrchestratorCtx, reconstruct_style
from pipeline.raster import SyntheticRasterProvider
from tests.test_orchestrator import OPTIONS, SHAPES, make_manifest
from worker import main as worker_main

LOGGER = logging.getLogger("test_worker_resume")

MD5_A = "aa" * 16
MD5_B = "bb" * 16
SOURCE_URL = "https://www.myfonts.com/collections/synthetic-test"


# ---------------------------------------------------------------------------
# scenario A: orchestrator crash/resume
# ---------------------------------------------------------------------------

def test_crash_after_first_freeze_then_resume_without_reacquire(tmp_path):
    provider = SyntheticRasterProvider(SHAPES)
    frozen_events = {"n": 0}

    def stage_cb(stage, detail):
        if detail.get("event") == "glyph_frozen":
            frozen_events["n"] += 1
            if frozen_events["n"] == 1:
                raise RuntimeError("simulated worker crash")

    ctx = OrchestratorCtx(
        cfg=None,
        cache=CacheStore(tmp_path / "cache", "1"),
        raster=provider,
        cancel_check=lambda: False,
        stage_cb=stage_cb,
        budget_deadline=time.monotonic() + 600,
    )
    manifest = make_manifest()
    metrics = estimate_from_manifest(manifest, {})
    with pytest.raises(RuntimeError, match="simulated worker crash"):
        asyncio.run(
            reconstruct_style(ctx, "md5:" + "ab" * 16, OPTIONS, "Fam", "Regular", manifest, metrics)
        )

    lookup = ctx.cache.lookup("md5:" + "ab" * 16, OPTIONS)
    assert lookup.status == "partial"
    assert lookup.frozen_glyphs >= 1
    frozen_prior = ctx.cache.load_frozen_glyphs(lookup.dir)
    frozen_gids = {d["gid"] for d in frozen_prior}
    assert frozen_gids, "crash checkpoint must contain the frozen glyph"
    # the crashed run did acquire fast-lane observations for the frozen glyph
    assert any(gid in frozen_gids for (gid, *_rest) in provider.calls)

    # resume with a fresh provider: frozen glyphs must NOT be re-acquired
    provider2 = SyntheticRasterProvider(SHAPES)
    ctx2 = OrchestratorCtx(
        cfg=None,
        cache=ctx.cache,
        raster=provider2,
        cancel_check=lambda: False,
        stage_cb=None,
        budget_deadline=time.monotonic() + 600,
    )
    res = asyncio.run(
        reconstruct_style(ctx2, "md5:" + "ab" * 16, OPTIONS, "Fam", "Regular", manifest, metrics)
    )
    assert res.ok, res.error
    assert res.glyphs_frozen == 3
    assert res.validation["passed"] is True
    for key in provider2.calls:
        assert key[0] not in frozen_gids, f"frozen glyph {key[0]} was re-acquired on resume"
    # non-frozen glyphs were reconstructed from fresh acquisitions
    assert any(key[0] not in frozen_gids for key in provider2.calls)
    assert ctx.cache.lookup("md5:" + "ab" * 16, OPTIONS).status == "binary"


# ---------------------------------------------------------------------------
# scenario B: worker-level persistence + restart + requeue
# ---------------------------------------------------------------------------

@pytest.fixture()
def worker_env(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    cfg.ensure_dirs()
    conn = open_db(cfg.data_root / "db" / "a23font.db")
    yield cfg, conn
    conn.close()


def _synthetic_runner(style_specs, on_style_done=None):
    async def runner(wctx):
        rows = services.register_styles(wctx.conn, wctx.job_id, style_specs)
        wctx.set_stage("DISCOVERING")
        provider = SyntheticRasterProvider(SHAPES)
        cache = CacheStore(
            wctx.cfg.data_root / "cache" / "pipeline", wctx.cfg.pipeline_version
        )
        for row in rows:
            wctx.check_cancel()  # between styles: cancel/shutdown honored
            services.set_style_status(wctx.conn, row["id"], "RECONSTRUCTING")
            t0 = time.monotonic()
            manifest = make_manifest(row["md5"])
            metrics = estimate_from_manifest(manifest, {})

            def cancel_check():
                wctx.check_cancel()  # raises CancelledWork / WorkerShutdown
                return False

            def stage_cb(stage, detail):
                services.set_stage(wctx.conn, wctx.job_id, stage)
                services.touch_heartbeat(wctx.conn, wctx.job_id, wctx.worker_id)

            octx = OrchestratorCtx(
                cfg=wctx.cfg,
                cache=cache,
                raster=provider,
                cancel_check=cancel_check,
                stage_cb=stage_cb,
                budget_deadline=time.monotonic() + 300,
            )
            res = await reconstruct_style(
                octx,
                "md5:" + row["md5"],
                OPTIONS,
                "Synthetic",
                row["name"],
                manifest,
                metrics,
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            if res.ok:
                services.set_style_status(
                    wctx.conn,
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
                    wctx.conn,
                    row["id"],
                    "FAILED",
                    error_code="STYLE_BUILD_FAILED",
                    error_message=res.error,
                    duration_ms=duration_ms,
                )
            if on_style_done is not None:
                on_style_done(wctx, row)

    return runner


def _events(conn, job_id):
    return [
        row["event"]
        for row in conn.execute(
            "SELECT event FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
        )
    ]


def test_worker_persistence_restart_and_requeue(worker_env):
    cfg, conn = worker_env
    job = services.create_job(conn, SOURCE_URL, False, cfg)
    job_id = job["id"]

    claimed = worker_main.claim_next_queued(conn, "w-test1", cfg)
    assert claimed == job_id
    job = services.get_job(conn, job_id)
    specs = [{"name": "Synthetic Regular", "md5": MD5_A, "source_identity": "md5:" + MD5_A}]
    final = asyncio.run(
        worker_main.run_job(conn, cfg, job, "w-test1", job_runner=_synthetic_runner(specs), logger=LOGGER)
    )
    assert final == "DONE"

    row = services.get_job(conn, job_id)
    assert row["status"] == "DONE"
    assert row["styles_total"] == 1 and row["styles_done"] == 1 and row["styles_failed"] == 0
    assert row["worker_heartbeat"] is not None
    report = json.loads(row["report_json"])
    # M5.A3: resource observations recorded in the job report
    assert report["max_rss_kb"] is not None and report["max_rss_kb"] > 0
    assert report["duration_s"] >= 0.0
    assert "mem_available_kb" in report  # None on hosts without /proc/meminfo
    assert report["styles"][0]["status"] == "DONE"
    assert report["styles"][0]["duration_ms"] is not None

    style_rows = conn.execute(
        "SELECT * FROM style_jobs WHERE job_id = ? ORDER BY position", (job_id,)
    ).fetchall()
    assert len(style_rows) == 1
    assert style_rows[0]["status"] == "DONE"
    assert style_rows[0]["cache_hit"] == 0  # first build: fresh reconstruction
    events = _events(conn, job_id)
    assert "claimed" in events and "finished" in events

    # --- simulate a worker restart mid-job -------------------------------
    conn.execute(
        "UPDATE jobs SET status = 'RESOLVING', stage = 'RESOLVING', worker_id = 'w-old',"
        " worker_heartbeat = '2020-01-01T00:00:00+00:00', finished_at = NULL WHERE id = ?",
        (job_id,),
    )
    conn.commit()
    requeued = services.requeue_stale_jobs(conn, "w-new", stale_after_s=90)
    assert requeued == 1
    row = services.get_job(conn, job_id)
    assert row["status"] == "QUEUED"
    assert row["stage"] is None and row["worker_id"] is None and row["worker_heartbeat"] is None
    assert row["attempts"] == 2  # claim #1 + requeue
    assert "requeued_stale" in _events(conn, job_id)

    # fresh heartbeat of another worker is NOT stale
    assert services.requeue_stale_jobs(conn, "w-new", stale_after_s=90) == 0

    # --- rerun after restart: style history preserved, cache serves -------
    claimed = worker_main.claim_next_queued(conn, "w-new", cfg)
    assert claimed == job_id
    job = services.get_job(conn, job_id)
    final = asyncio.run(
        worker_main.run_job(conn, cfg, job, "w-new", job_runner=_synthetic_runner(specs), logger=LOGGER)
    )
    assert final == "DONE"
    style_rows = conn.execute(
        "SELECT * FROM style_jobs WHERE job_id = ? ORDER BY position", (job_id,)
    ).fetchall()
    assert len(style_rows) == 1  # no duplicate registration on resume
    assert style_rows[0]["status"] == "DONE"
    assert style_rows[0]["cache_hit"] == 1  # binary cache hit on the rerun
    row = services.get_job(conn, job_id)
    assert row["status"] == "DONE"
    assert row["styles_total"] == 1 and row["styles_done"] == 1
    assert row["attempts"] == 3  # claim #1 + requeue + claim #2


# ---------------------------------------------------------------------------
# cancel between styles
# ---------------------------------------------------------------------------

def test_cancel_between_styles_ends_cancelled(worker_env):
    cfg, conn = worker_env
    job = services.create_job(conn, SOURCE_URL, False, cfg)
    job_id = job["id"]
    claimed = worker_main.claim_next_queued(conn, "w-test1", cfg)
    assert claimed == job_id
    job = services.get_job(conn, job_id)

    specs = [
        {"name": "Style A", "md5": MD5_A, "source_identity": "md5:" + MD5_A},
        {"name": "Style B", "md5": MD5_B, "source_identity": "md5:" + MD5_B},
    ]

    def cancel_after_first(wctx, row):
        services.request_cancel(wctx.conn, wctx.job_id)  # user cancels mid-run

    final = asyncio.run(
        worker_main.run_job(
            conn, cfg, job, "w-test1",
            job_runner=_synthetic_runner(specs, on_style_done=cancel_after_first),
            logger=LOGGER,
        )
    )
    assert final == "CANCELLED"
    row = services.get_job(conn, job_id)
    assert row["status"] == "CANCELLED"
    assert row["cancel_requested"] == 1
    report = json.loads(row["report_json"])
    assert report["max_rss_kb"] is not None  # resources recorded on cancel too

    style_rows = conn.execute(
        "SELECT name, status FROM style_jobs WHERE job_id = ? ORDER BY position", (job_id,)
    ).fetchall()
    assert style_rows[0]["status"] == "DONE"     # style A finished before cancel
    assert style_rows[1]["status"] == "QUEUED"   # style B never started
    events = _events(conn, job_id)
    assert "cancel_requested" in events and "finished" in events

# ---------------------------------------------------------------------------
# offline honesty: default production runner refuses live network (T-007 flag)
# ---------------------------------------------------------------------------

def test_default_runner_fails_network_disabled_when_offline(worker_env):
    cfg, conn = worker_env
    assert cfg.pipeline_live is False
    job = services.create_job(
        conn, "https://www.myfonts.com/collections/network-gate", False, cfg
    )
    job_id = job["id"]
    claimed = worker_main.claim_next_queued(conn, "w-test1", cfg)
    assert claimed == job_id
    final = asyncio.run(
        worker_main.run_job(
            conn, cfg, services.get_job(conn, job_id), "w-test1", logger=LOGGER
        )
    )
    assert final == "FAILED"
    row = services.get_job(conn, job_id)
    assert row["status"] == "FAILED"
    assert row["error_code"] == "NETWORK_DISABLED"
    assert "A23FONT_PIPELINE_LIVE" in row["error_message"]