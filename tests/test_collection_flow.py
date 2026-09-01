"""M6 multi-style collection flow (worker level, offline synthetic).

Reuses the T-006 synthetic style infrastructure (SyntheticRasterProvider +
3-glyph manifests + the real orchestrator) and the SAME worker loop the
production runner uses (worker.main.run_styles):

  * partial-failure isolation: 3 styles, style #2 force-failed via the
    A23FONT_FORCE_FAIL_STYLES diagnostic hook -> job DONE_WITH_ERRORS, zip
    built with fonts for styles 1+3 only and reports for all three;
  * all styles fail -> job FAILED, NO zip (mandate: no zip for zero
    successes);
  * cancel between styles -> job CANCELLED, zip NOT built.

Note on the hollow guard: production refuses font binaries <= 20 KB
(MIN_FONT_BYTES, proven at full threshold in tests/test_pack.py). The
synthetic 3-glyph test fonts are ~1.4 KB, so the flow tests lower the module
constant via monkeypatch - the flow semantics under test are isolation,
packaging layout and job status, not font size.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import zipfile

import pytest

from app import services
from app.config import Config
from app.db import open_db
from pipeline import pack
from pipeline.cache import CacheStore
from pipeline.metrics import estimate_from_manifest
from pipeline.orchestrator import OrchestratorCtx, reconstruct_style
from pipeline.raster import SyntheticRasterProvider
from tests.test_orchestrator import OPTIONS, SHAPES, make_manifest
from worker import main as worker_main

LOGGER = logging.getLogger("test_collection_flow")

SOURCE_URL = "https://www.myfonts.com/collections/synthetic-collection"
FAMILY = "Synthetic"
MD5_1 = "aa" * 16
MD5_2 = "bb" * 16
MD5_3 = "cc" * 16


def _specs():
    return [
        {"name": "Regular", "md5": MD5_1, "source_identity": "md5:" + MD5_1},
        {"name": "Bold", "md5": MD5_2, "source_identity": "md5:" + MD5_2},
        {"name": "Thin", "md5": MD5_3, "source_identity": "md5:" + MD5_3},
    ]


@pytest.fixture()
def worker_env(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "data")
    cfg.ensure_dirs()
    conn = open_db(cfg.data_root / "db" / "a23font.db")
    # synthetic fonts are real but tiny (3 glyphs); lower the hollow guard
    # (production default is 20000 - see module docstring)
    monkeypatch.setattr(pack, "MIN_FONT_BYTES", 100)
    yield cfg, conn
    conn.close()


def _collection_runner(on_style_done=None):
    """Synthetic multi-style runner on the shared worker loop (run_styles).

    Each style is reconstructed for real by the orchestrator from synthetic
    raster observations, exactly like tests/test_worker_resume.py does.
    """

    async def runner(wctx):
        provider = SyntheticRasterProvider(SHAPES)
        cache = CacheStore(
            wctx.cfg.data_root / "cache" / "pipeline", wctx.cfg.pipeline_version
        )

        async def exec_style(row):
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
                FAMILY,
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
                        "family": FAMILY,
                        "glyphs_total": res.glyphs_total,
                        "glyphs_frozen": res.glyphs_frozen,
                        "glyphs_failed": res.glyphs_failed,
                        "cache_hit": res.cache_hit,
                        "validation_passed": bool(res.validation.get("passed")),
                    },
                )
            else:
                services.set_style_status(
                    wctx.conn,
                    row["id"],
                    "FAILED",
                    error_code=worker_main.classify_style_error(res.error),
                    error_message=res.error,
                    duration_ms=duration_ms,
                    report_json={
                        "family": FAMILY,
                        "glyphs_frozen": res.glyphs_frozen,
                        "error": res.error,
                    },
                )
            if on_style_done is not None:
                on_style_done(wctx, row)

        await worker_main.run_styles(wctx, _specs(), exec_style, family_name=FAMILY)

    return runner


def _run(cfg, conn, runner):
    job = services.create_job(conn, SOURCE_URL, False, cfg)
    job_id = job["id"]
    assert worker_main.claim_next_queued(conn, "w-coll", cfg) == job_id
    final = asyncio.run(
        worker_main.run_job(
            conn, cfg, services.get_job(conn, job_id), "w-coll",
            job_runner=runner, logger=LOGGER,
        )
    )
    return job_id, final


# ---------------------------------------------------------------------------
# partial failure: forced style #2 failure -> DONE_WITH_ERRORS + zip
# ---------------------------------------------------------------------------

def test_partial_failure_isolation_and_zip(worker_env):
    cfg, conn = worker_env
    cfg.force_fail_styles = "Bold"  # A23FONT_FORCE_FAIL_STYLES hook

    job_id, final = _run(cfg, conn, _collection_runner())
    assert final == "DONE_WITH_ERRORS"

    row = services.get_job(conn, job_id)
    assert row["status"] == "DONE_WITH_ERRORS"
    assert row["styles_total"] == 3
    assert row["styles_done"] == 2 and row["styles_failed"] == 1

    style_rows = conn.execute(
        "SELECT name, status, error_code FROM style_jobs"
        " WHERE job_id = ? ORDER BY position",
        (job_id,),
    ).fetchall()
    assert [(r["name"], r["status"]) for r in style_rows] == [
        ("Regular", "DONE"),
        ("Bold", "FAILED"),
        ("Thin", "DONE"),
    ]
    assert style_rows[1]["error_code"] == "FORCED_TEST_FAILURE"

    # zip artifact recorded on the job row + present on disk
    zip_path = cfg.data_root / "outputs" / f"{job_id}.zip"
    assert row["zip_name"] == zip_path.name
    assert row["zip_size"] == zip_path.stat().st_size
    assert zip_path.is_file()

    zf = zipfile.ZipFile(zip_path)
    names = zf.namelist()
    fonts = sorted(n for n in names if n.startswith("fonts/"))
    reports = sorted(n for n in names if n.startswith("reports/"))
    # fonts ONLY for styles 1+3 (ttf+otf each); reports for all three
    assert len(fonts) == 4
    assert all("Bold" not in n for n in fonts)
    assert any("Regular.ttf" in n for n in fonts) and any("Thin.otf" in n for n in fonts)
    assert len(reports) == 3

    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["styles_total"] == 3
    assert manifest["styles_done"] == 2 and manifest["styles_failed"] == 1
    assert manifest["validation_summary"] == {"done": 2, "failed": 1}
    assert manifest["collection_name"] == FAMILY
    assert manifest["source_url"] == SOURCE_URL
    by_name = {s["name"]: s for s in manifest["styles"]}
    assert by_name["Bold"]["status"] == "FAILED"
    assert by_name["Bold"]["error_code"] == "FORCED_TEST_FAILURE"
    assert by_name["Bold"]["ttf_sha256"] is None
    assert by_name["Regular"]["status"] == "DONE"
    assert by_name["Regular"]["ttf_sha256"] and by_name["Regular"]["otf_sha256"]
    assert by_name["Regular"]["glyph_frozen"] == 3

    # job report carries the packaging block
    report = json.loads(row["report_json"])
    assert report["packaging"]["zip_name"] == zip_path.name
    assert report["packaging"]["zip_bytes"] == zip_path.stat().st_size


# ---------------------------------------------------------------------------
# all styles fail -> FAILED, NO zip (mandate: no zip for zero successes)
# ---------------------------------------------------------------------------

def test_all_styles_failed_means_failed_job_without_zip(worker_env):
    cfg, conn = worker_env
    cfg.force_fail_styles = "Regular;Bold;Thin"  # every style force-failed

    job_id, final = _run(cfg, conn, _collection_runner())
    assert final == "FAILED"

    row = services.get_job(conn, job_id)
    assert row["status"] == "FAILED"
    assert row["error_code"] == "STYLES_FAILED"
    assert row["styles_done"] == 0 and row["styles_failed"] == 3
    assert row["zip_name"] is None and row["zip_size"] is None
    assert not (cfg.data_root / "outputs" / f"{job_id}.zip").exists()

    style_rows = conn.execute(
        "SELECT error_code FROM style_jobs WHERE job_id = ?", (job_id,)
    ).fetchall()
    assert [r["error_code"] for r in style_rows] == ["FORCED_TEST_FAILURE"] * 3


# ---------------------------------------------------------------------------
# cancel between styles -> CANCELLED, zip NOT built
# ---------------------------------------------------------------------------

def test_cancel_between_styles_cancels_without_zip(worker_env):
    cfg, conn = worker_env

    def cancel_after_first(wctx, row):
        services.request_cancel(wctx.conn, wctx.job_id)  # user cancels mid-run

    job_id, final = _run(cfg, conn, _collection_runner(on_style_done=cancel_after_first))
    assert final == "CANCELLED"

    row = services.get_job(conn, job_id)
    assert row["status"] == "CANCELLED"
    assert row["cancel_requested"] == 1
    assert row["zip_name"] is None
    assert not (cfg.data_root / "outputs" / f"{job_id}.zip").exists()

    style_rows = conn.execute(
        "SELECT name, status FROM style_jobs WHERE job_id = ? ORDER BY position",
        (job_id,),
    ).fetchall()
    assert style_rows[0]["status"] == "DONE"    # style 1 finished pre-cancel
    assert style_rows[1]["status"] == "QUEUED"  # style 2 never started
    assert style_rows[2]["status"] == "QUEUED"  # style 3 never started


# ---------------------------------------------------------------------------
# A23FONT_MAX_STYLES truncation knob
# ---------------------------------------------------------------------------

def test_max_styles_truncates_with_audit_event(worker_env):
    cfg, conn = worker_env
    cfg.max_styles = 2  # keep only the first two styles

    job_id, final = _run(cfg, conn, _collection_runner())
    assert final == "DONE"

    row = services.get_job(conn, job_id)
    assert row["styles_total"] == 2 and row["styles_done"] == 2
    events = [
        (r["event"], r["detail_json"])
        for r in conn.execute(
            "SELECT event, detail_json FROM job_events WHERE job_id = ? ORDER BY id",
            (job_id,),
        )
    ]
    truncated = [detail for event, detail in events if event == "styles_truncated"]
    assert len(truncated) == 1
    detail = json.loads(truncated[0])
    assert detail["note"] == "styles truncated"
    assert detail["available"] == 3 and detail["kept"] == 2