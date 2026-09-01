"""M5 orchestrator tests (offline): synthetic 3-glyph style through the full
fast15 stage machine — fast lane freeze, binary cache reuse with zero
provider acquisitions, budget-exceeded semantics, fontmodel cache route, and
the honest vietnamese milestone failure.

Glyphs: "O" (ring, 1 component + 1 hole), "H" (rect union, sharp corners),
"." (small dot). All advances come from manifest layout meta.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from pipeline.cache import CacheStore
from pipeline.metrics import estimate_from_manifest
from pipeline.models import GlyphManifest
from pipeline.orchestrator import (
    OrchestratorCtx,
    PipelineNotImplementedError,
    reconstruct_style,
)
from pipeline.raster import SyntheticRasterProvider

MD5 = "ab" * 16
IDENTITY = "md5:" + MD5
OPTIONS = {"vietnamese": False}


# ---------------------------------------------------------------------------
# synthetic glyph shapes (fractions of the em cell; baseline at 0.8*size)
# ---------------------------------------------------------------------------

def _paint(canvas: np.ndarray, draw_fn) -> None:
    h, w = canvas.shape
    img = Image.new("L", (w, h), 255)  # white background, ink = black
    draw_fn(ImageDraw.Draw(img), w, h)
    arr = 1.0 - np.asarray(img, dtype=np.float32) / 255.0
    np.copyto(canvas, arr)


def _draw_o(d, w, h):
    d.ellipse([0.12 * w, 0.10 * h, 0.78 * w, 0.80 * h], fill=0)
    d.ellipse([0.24 * w, 0.22 * h, 0.66 * w, 0.68 * h], fill=255)  # hole


def _draw_h(d, w, h):
    d.rectangle([0.10 * w, 0.10 * h, 0.24 * w, 0.80 * h], fill=0)
    d.rectangle([0.60 * w, 0.10 * h, 0.74 * w, 0.80 * h], fill=0)
    d.rectangle([0.24 * w, 0.40 * h, 0.60 * w, 0.50 * h], fill=0)


def _draw_dot(d, w, h):
    d.ellipse([0.20 * w, 0.66 * h, 0.36 * w, 0.80 * h], fill=0)


SHAPES = {
    "gid_o": lambda canvas: _paint(canvas, _draw_o),
    "gid_h": lambda canvas: _paint(canvas, _draw_h),
    "gid_dot": lambda canvas: _paint(canvas, _draw_dot),
}


def make_manifest(md5: str = MD5) -> GlyphManifest:
    entries = [
        {"gid": "gid_o", "cp": 79, "meta": {"advance": 900}},
        {"gid": "gid_h", "cp": 72, "meta": {"advance": 840}},
        {"gid": "gid_dot", "cp": 46, "meta": {"advance": 360}},
    ]
    return GlyphManifest(
        md5=md5,
        total_glyphs=len(entries),
        unicode_coverage=[46, 72, 79],
        pages=1,
        stop_reason="short_page",
        entries=entries,
        notes=["synthetic manifest"],
    )


def make_ctx(tmp_path, provider, deadline=None) -> OrchestratorCtx:
    cfg = SimpleNamespace(data_root=tmp_path, pipeline_version="1")
    cache = CacheStore(tmp_path / "cache", "1")
    return OrchestratorCtx(
        cfg=cfg,
        cache=cache,
        raster=provider,
        cancel_check=lambda: False,
        stage_cb=None,
        budget_deadline=deadline if deadline is not None else time.monotonic() + 600,
    )


def run(tmp_path, provider, options=OPTIONS, identity=IDENTITY, deadline=None):
    ctx = make_ctx(tmp_path, provider, deadline)
    manifest = make_manifest()
    metrics = estimate_from_manifest(manifest, {})
    result = asyncio.run(
        reconstruct_style(ctx, identity, options, "TestFamily", "Regular", manifest, metrics)
    )
    return ctx, result


# ---------------------------------------------------------------------------
# full offline reconstruction + binary cache reuse
# ---------------------------------------------------------------------------

def test_offline_reconstruction_freezes_all_glyphs(tmp_path):
    provider = SyntheticRasterProvider(SHAPES)
    ctx, res = run(tmp_path, provider)
    assert res.ok, res.error
    assert res.glyphs_total == 3
    assert res.glyphs_frozen == 3
    assert res.glyphs_failed == 0
    assert res.failed_glyphs == []
    # confidence pass counts: all three pass in the fast lane
    assert res.report["pass_counts"]["fast_lane"] == 3
    assert res.report["budget_exceeded"] is False
    assert res.report["kerning"] == "typography inference milestone pending"
    assert res.ttf.is_file() and res.otf.is_file()
    assert res.validation["passed"] is True
    assert res.ttf.read_bytes()[:4] == b"\x00\x01\x00\x00"  # TTF sfnt
    assert res.otf.read_bytes()[:4] == b"OTTO"
    # fast lane only: 2 observations (1024 x0, 2048 x0) per glyph
    for gid in ("gid_o", "gid_h", "gid_dot"):
        assert provider.calls.get((gid, 1024, 0.0, 0.0)) == 1
        assert provider.calls.get((gid, 2048, 0.0, 0.0)) == 1
    assert ctx.cache.lookup(IDENTITY, OPTIONS).status == "binary"


def test_second_run_is_binary_cache_hit_with_zero_acquisitions(tmp_path):
    provider = SyntheticRasterProvider(SHAPES)
    run(tmp_path, provider)

    provider2 = SyntheticRasterProvider(SHAPES)
    ctx2, res2 = run(tmp_path, provider2)
    assert res2.ok, res2.error
    assert res2.cache_hit == "binary"
    assert res2.ttf.is_file() and res2.otf.is_file()
    assert res2.validation.get("passed") is True
    # zero provider acquisitions on a binary cache hit
    assert provider2.calls == {}
    assert ctx2.cache.lookup(IDENTITY, OPTIONS).status == "binary"


def test_fontmodel_cache_route_rebuilds_binaries(tmp_path):
    provider = SyntheticRasterProvider(SHAPES)
    ctx, res = run(tmp_path, provider)
    assert res.ok
    entry_dir = ctx.cache.dir_for(IDENTITY, OPTIONS)
    (entry_dir / "final.ttf").unlink()
    (entry_dir / "final.otf").unlink()
    assert ctx.cache.lookup(IDENTITY, OPTIONS).status == "fontmodel"

    provider2 = SyntheticRasterProvider(SHAPES)
    ctx2, res2 = run(tmp_path, provider2)
    assert res2.ok, res2.error
    assert res2.cache_hit == "fontmodel"
    assert res2.ttf.is_file() and res2.otf.is_file()
    assert provider2.calls == {}  # no re-acquisition from the fontmodel route
    assert ctx2.cache.lookup(IDENTITY, OPTIONS).status == "binary"


# ---------------------------------------------------------------------------
# budget semantics
# ---------------------------------------------------------------------------

def test_budget_exceeded_still_finalizes_fast_lane_glyphs(tmp_path):
    provider = SyntheticRasterProvider(SHAPES)
    ctx, res = run(tmp_path, provider, deadline=time.monotonic() - 1.0)  # past
    assert res.ok, res.error
    assert res.report["budget_exceeded"] is True
    # fast-lane glyphs still freeze; only optional refinement stops
    assert res.glyphs_frozen == 3
    assert res.validation["passed"] is True


# ---------------------------------------------------------------------------
# honest milestone failures
# ---------------------------------------------------------------------------

def test_vietnamese_option_raises_honest_pending_error(tmp_path):
    provider = SyntheticRasterProvider(SHAPES)
    ctx = make_ctx(tmp_path, provider)
    manifest = make_manifest()
    metrics = estimate_from_manifest(manifest, {})
    with pytest.raises(PipelineNotImplementedError, match="vietnamese"):
        asyncio.run(
            reconstruct_style(
                ctx, IDENTITY, {"vietnamese": True}, "TestFamily", "Regular", manifest, metrics
            )
        )


# ---------------------------------------------------------------------------
# T-007 live failure class: hollow fonts must never be served as success
# ---------------------------------------------------------------------------

def test_zero_usable_observations_fails_honest_no_glyphs_frozen(tmp_path):
    provider = SyntheticRasterProvider({})  # no shapes: every acquire "missing"
    ctx = make_ctx(tmp_path, provider)
    manifest = make_manifest()
    metrics = estimate_from_manifest(manifest, {})
    res = asyncio.run(
        reconstruct_style(ctx, IDENTITY, OPTIONS, "Fam", "Regular", manifest, metrics)
    )
    assert res.ok is False
    assert res.error == "NO_GLYPHS_FROZEN"
    assert res.glyphs_frozen == 0
    assert res.glyphs_total == 3
    assert res.ttf is None and res.otf is None
    # entry was invalidated, not left as a reusable success
    assert ctx.cache.lookup(IDENTITY, OPTIONS).status == "miss"


def test_hollow_binary_cache_entry_is_invalidated_not_served(tmp_path):
    # fabricate a hollow "binary" entry: success ledger + final bytes but zero
    # frozen glyphs (exactly what the T-007 identity bug produced live)
    cache = CacheStore(tmp_path / "cache", "1")
    entry = cache.begin(IDENTITY, OPTIONS, {"family": "Fam", "style": "Regular"})
    cache.checkpoint_glyphs(entry, [], "hollow")
    cache.save_final(entry, b"\x00\x01", b"\x00\x01", {"passed": True})
    probe = cache.lookup(IDENTITY, OPTIONS)
    assert probe.status == "binary" and probe.frozen_glyphs == 0

    provider = SyntheticRasterProvider(SHAPES)
    ctx = make_ctx(tmp_path, provider)
    manifest = make_manifest()
    metrics = estimate_from_manifest(manifest, {})
    res = asyncio.run(
        reconstruct_style(ctx, IDENTITY, OPTIONS, "Fam", "Regular", manifest, metrics)
    )
    assert res.ok, res.error
    assert res.cache_hit is None  # hollow hit invalidated -> real rebuild
    assert res.glyphs_frozen == 3
    assert res.validation["passed"] is True
