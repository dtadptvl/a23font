"""fast15 stage machine for ONE font style (milestone M5, offline slice).

Implements the fast15.md critical path per style:

    EXACT CACHE probe (binary | fontmodel | partial | miss)
      -> progressive per-glyph reconstruction:
           FAST LANE (1024 x0, 2048 x0) -> geometry pass -> confidence
           -> refinement ladder levels 1..4 (only missing observations)
           -> bounded LOCAL OPTIMIZER (failing glyph only, never whole font)
           -> freeze + batched checkpoint (CHECKPOINT_BATCH)
      -> .notdef + space guarantee
      -> heavy validation ONCE on temporary binaries
      -> final TTF+OTF from the canonical FontModel + cache.save_final

Honesty rules (never faked):
  * vietnamese option -> PipelineNotImplementedError (VIETNAMESE_PENDING)
  * kerning/features  -> skipped, note "typography inference milestone pending"
  * cancellation     -> CancelledWork (checkpoint persisted before raise)
"""
from __future__ import annotations

import json
import logging
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from pipeline import orchestrator_refine as refine
from pipeline.build import build_otf, build_ttf
from pipeline.cache import CHECKPOINT_BATCH, CacheStore
from pipeline.fontmodel import FontModel, GlyphModel, suggest_glyph_name
from pipeline.metrics import MetricsEstimate
from pipeline.raster import ObservationRequest
from pipeline.validate import fonttools_validate, heavy_validation

__all__ = [
    "PipelineNotImplementedError",
    "CancelledWork",
    "StyleResult",
    "OrchestratorCtx",
    "reconstruct_style",
]

LOGGER = logging.getLogger(__name__)

FAST_LANE = ((1024, 0.0), (2048, 0.0))
_NOTDEF_NOTE = "typography inference milestone pending"


class PipelineNotImplementedError(RuntimeError):
    """Honest failure for pipeline capabilities that are pending milestones."""


class CancelledWork(RuntimeError):
    """Raised when the job cancel flag is observed; checkpoints persist."""


@dataclass
class StyleResult:
    ok: bool
    cache_hit: Optional[str]  # "binary" | "fontmodel" | None
    glyphs_total: int
    glyphs_frozen: int
    glyphs_failed: int
    failed_glyphs: List[str]
    ttf: Optional[Path]
    otf: Optional[Path]
    validation: Dict[str, Any]
    report: Dict[str, Any]
    duration_s: float
    error: Optional[str] = None


@dataclass
class OrchestratorCtx:
    cfg: Any
    cache: CacheStore
    raster: Any  # RasterProvider-like with async acquire(md5, req)
    cancel_check: Callable[[], bool]
    stage_cb: Optional[Callable[[str, Dict[str, Any]], None]] = None
    budget_deadline: float = float("inf")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _emit(ctx: OrchestratorCtx, stage: str, detail: Dict[str, Any]) -> None:
    if ctx.stage_cb is None:
        return
    ctx.stage_cb(stage, detail)


def _check_cancel(ctx: OrchestratorCtx) -> None:
    if ctx.cancel_check():
        raise CancelledWork("cancel requested")


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _glyph_name(gid: str, cp: int) -> str:
    if cp > 0:
        return suggest_glyph_name(cp)
    cleaned = "".join(c if (c.isalnum() or c in "_-") else "_" for c in str(gid))
    return f"gid_{cleaned}" if cleaned else "gid"


def _result_duration(t0: float) -> float:
    return round(time.monotonic() - t0, 3)


# ---------------------------------------------------------------------------
# cache-hit routes
# ---------------------------------------------------------------------------

def _binary_result(lookup, t0: float) -> StyleResult:
    report = _load_json(lookup.dir / "report.json")
    return StyleResult(
        ok=True,
        cache_hit="binary",
        glyphs_total=int(lookup.frozen_glyphs),
        glyphs_frozen=int(lookup.frozen_glyphs),
        glyphs_failed=0,
        failed_glyphs=[],
        ttf=lookup.dir / "final.ttf",
        otf=lookup.dir / "final.otf",
        validation=dict(report),
        report={"cache_hit": "binary", "validation": dict(report)},
        duration_s=_result_duration(t0),
        error=None,
    )


def _fontmodel_result(
    ctx: OrchestratorCtx, lookup, family: str, style_name: str, t0: float
) -> Optional[StyleResult]:
    """fontmodel hit: rebuild the missing binaries + cheap structural check."""
    data = _load_json(lookup.dir / "fontmodel.json")
    if not data:
        return None
    model = FontModel.from_dict(data)
    with tempfile.TemporaryDirectory(prefix="a23font_fm_") as tmp:
        ttf_path = str(Path(tmp) / "probe.ttf")
        otf_path = str(Path(tmp) / "probe.otf")
        build_ttf(model, ttf_path)
        build_otf(model, otf_path)
        check_ttf = fonttools_validate(ttf_path)
        check_otf = fonttools_validate(otf_path)
        if not (check_ttf.get("passed") and check_otf.get("passed")):
            ctx.cache.invalidate(
                lookup.dir,
                "fontmodel structural check failed: "
                + "; ".join(list(check_ttf.get("errors", [])) + list(check_otf.get("errors", [])))[:400],
            )
            return None
        ttf_bytes = Path(ttf_path).read_bytes()
        otf_bytes = Path(otf_path).read_bytes()
    report = {
        "passed": True,
        "fonttools_ttf": check_ttf,
        "fonttools_otf": check_otf,
        "harfbuzz": {"passed": True, "skipped": True, "details": "fontmodel cache route: heavy engines not re-run", "shaped": []},
        "freetype": {"passed": True, "skipped": True, "details": "fontmodel cache route: heavy engines not re-run", "ink_glyphs": 0, "empty_glyphs": 0},
        "skipped_engines": ["harfbuzz", "freetype"],
    }
    ctx.cache.save_final(lookup.dir, ttf_bytes, otf_bytes, report)
    return StyleResult(
        ok=True,
        cache_hit="fontmodel",
        glyphs_total=len(model.glyphs),
        glyphs_frozen=int(lookup.frozen_glyphs),
        glyphs_failed=0,
        failed_glyphs=[],
        ttf=lookup.dir / "final.ttf",
        otf=lookup.dir / "final.otf",
        validation=report,
        report={"cache_hit": "fontmodel", "validation": report},
        duration_s=_result_duration(t0),
        error=None,
    )


# ---------------------------------------------------------------------------
# per-glyph reconstruction
# ---------------------------------------------------------------------------

async def _acquire(
    ctx: OrchestratorCtx,
    identity: str,
    gid: str,
    cp: int,
    size_px: int,
    x_phase: float,
    observations: Dict[Tuple[int, float, float], Any],
) -> None:
    key = (int(size_px), float(x_phase), 0.0)
    if key in observations:
        return
    req = ObservationRequest(gid=gid, cp=cp, size_px=int(size_px), x_phase=float(x_phase), y_phase=0.0)
    observations[key] = await ctx.raster.acquire(identity, req)


def _usable_count(observations: Dict[Tuple[int, float, float], Any]) -> int:
    return sum(1 for obs in observations.values() if obs is not None and obs.alpha is not None)


def _freeze_glyph(
    model: FontModel,
    gid: str,
    cp: int,
    candidate,
    frozen_dicts: List[dict],
    note: str = "geometry",
) -> GlyphModel:
    glyph = GlyphModel(
        name=_glyph_name(gid, cp),
        unicode_cp=cp if cp > 0 else None,
        advance=int(round(float(candidate.advance))),
        contours=list(candidate.contours_units),
        status="RECONSTRUCTED",
        confidence=1.0,
    )
    model.add_glyph(glyph)
    frozen_dicts.append({"gid": gid, "cp": cp, "note": note, "glyph": glyph.to_dict()})
    return glyph


# ---------------------------------------------------------------------------
# the stage machine
# ---------------------------------------------------------------------------

async def reconstruct_style(
    ctx: OrchestratorCtx,
    style_identity: str,
    options: Dict[str, Any],
    family: str,
    style_name: str,
    manifest: Any,
    metrics: MetricsEstimate,
) -> StyleResult:
    """Reconstruct ONE style end-to-end (or serve it from the cache)."""
    t0 = time.monotonic()
    options = dict(options or {})
    _emit(ctx, "RECONSTRUCTING", {"event": "style_start", "style": style_name})

    lookup = ctx.cache.lookup(style_identity, options)
    if lookup.status == "binary":
        LOGGER.info("cache hit (binary) for %s", style_identity)
        return _binary_result(lookup, t0)

    if bool(options.get("vietnamese")):
        raise PipelineNotImplementedError("vietnamese extension milestone pending")

    if lookup.status == "fontmodel":
        result = _fontmodel_result(ctx, lookup, family, style_name, t0)
        if result is not None:
            return result
        lookup = ctx.cache.lookup(style_identity, options)  # invalidated -> miss

    entries = [e for e in (getattr(manifest, "entries", []) or []) if isinstance(e, dict)]

    if lookup.status == "partial":
        entry_dir = lookup.dir
        frozen_prior = ctx.cache.load_frozen_glyphs(entry_dir)
        LOGGER.info("cache hit (partial) for %s: %d frozen", style_identity, len(frozen_prior))
    else:
        entry_dir = ctx.cache.begin(
            style_identity, options, {"family": family, "style": style_name}
        )
        frozen_prior = []

    model = FontModel(upem=1000)
    model.metadata = {
        "familyName": family or "A23Font",
        "styleName": style_name or "Regular",
        "psName": f"{family or 'A23Font'}-{style_name or 'Regular'}",
        "fullName": f"{family or 'A23Font'} {style_name or 'Regular'}",
        "sourceIdentity": str(style_identity),
    }
    model.global_metrics["ascender"] = int(round(metrics.ascender_units))
    model.global_metrics["descender"] = int(round(metrics.descender_units))
    model.global_metrics["line_gap"] = 0
    model.global_metrics["unitsPerEm"] = 1000

    frozen_dicts: List[dict] = []
    frozen_by_gid: Dict[str, dict] = {}
    for prior in frozen_prior:
        if not isinstance(prior, dict) or "glyph" not in prior or "gid" not in prior:
            continue
        try:
            glyph = GlyphModel.from_dict(prior["glyph"])
        except (KeyError, TypeError, ValueError):
            LOGGER.warning("skipping corrupt frozen glyph %r", prior.get("gid"))
            continue
        model.add_glyph(glyph)
        frozen_dicts.append(prior)
        frozen_by_gid[str(prior["gid"])] = prior

    failed: Dict[str, str] = {}
    pass_counts = {"fast_lane": 0, "level_1": 0, "level_2": 0, "level_3": 0, "level_4": 0, "optimizer": 0}
    level_log: List[Dict[str, Any]] = []
    budget_exceeded = False
    since_checkpoint = 0

    def checkpoint(note: str) -> None:
        nonlocal since_checkpoint
        ctx.cache.checkpoint_glyphs(entry_dir, frozen_dicts, note)
        since_checkpoint = 0

    def freeze(gid: str, cp: int, candidate, via: str) -> None:
        nonlocal since_checkpoint
        _freeze_glyph(model, gid, cp, candidate, frozen_dicts, note=via)
        since_checkpoint += 1
        pass_counts[via] = pass_counts.get(via, 0) + 1
        _emit(
            ctx,
            "RECONSTRUCTING",
            {"event": "glyph_frozen", "gid": gid, "via": via, "frozen": len(frozen_dicts), "total": len(entries)},
        )
        if since_checkpoint >= CHECKPOINT_BATCH:
            checkpoint(f"frozen batch {len(frozen_dicts)}")

    try:
        for index, entry in enumerate(entries):
            _check_cancel(ctx)
            gid = str(entry.get("gid") or f"g{index}")
            try:
                cp = int(entry.get("cp") or 0)
            except (TypeError, ValueError):
                cp = 0

            if gid in frozen_by_gid:
                continue  # resumed from checkpoint; never re-acquire

            if time.monotonic() > ctx.budget_deadline:
                budget_exceeded = True

            # FAST LANE -------------------------------------------------
            observations: Dict[Tuple[int, float, float], Any] = {}
            for size, phase in FAST_LANE:
                await _acquire(ctx, style_identity, gid, cp, size, phase, observations)
            pack = refine.reconstruct_candidate(gid, cp, observations, metrics)
            if pack is None:
                failed[gid] = "no_usable_observations"
                continue
            report = refine.run_confidence(pack)
            if report.passed:
                freeze(gid, cp, pack.candidate, "fast_lane")
                continue

            if budget_exceeded:
                failed[gid] = "fast_lane_failed_budget:" + ",".join(report.failures)
                continue

            # REFINEMENT LADER (fast15 levels 1..4) -----------------------
            passed = False
            for level, reqs in refine.REFINEMENT_LEVELS:
                _check_cancel(ctx)
                if time.monotonic() > ctx.budget_deadline:
                    budget_exceeded = True
                    level_log.append({"gid": gid, "level": level, "result": "skipped_budget"})
                    break
                before = _usable_count(observations)
                for size, phase in reqs:
                    await _acquire(ctx, style_identity, gid, cp, size, phase, observations)
                acquired = _usable_count(observations) - before
                if acquired == 0:
                    level_log.append({"gid": gid, "level": level, "result": "skipped_not_supported"})
                    continue
                repack = refine.reconstruct_candidate(gid, cp, observations, metrics)
                if repack is None:
                    level_log.append({"gid": gid, "level": level, "result": "no_usable_observations"})
                    continue
                pack = repack
                report = refine.run_confidence(pack)
                if report.passed:
                    level_log.append({"gid": gid, "level": level, "result": "pass"})
                    freeze(gid, cp, pack.candidate, f"level_{level}")
                    passed = True
                    break
                level_log.append(
                    {"gid": gid, "level": level, "result": "fail", "failures": list(report.failures)}
                )
            if passed:
                continue

            # LOCAL OPTIMIZER (failing glyph only) ------------------------
            optimized = False
            opt = refine.local_optimizer(pack, metrics)
            if opt is not None:
                pack.candidate.contours_units = opt.contours_units
                pack.candidate.advance = opt.advance
                report = refine.run_confidence(pack)
                if report.passed:
                    freeze(gid, cp, pack.candidate, "optimizer")
                    optimized = True
            if optimized:
                continue
            failed[gid] = "FAILED_GLYPH:" + ",".join(report.failures or ["unknown"])
    except BaseException:
        # crash / cancel / shutdown: persist the cumulative frozen list so a
        # later run resumes without re-acquiring frozen glyphs
        if since_checkpoint > 0:
            try:
                checkpoint("checkpoint before exit")
            except Exception:  # noqa: BLE001 - never mask the original error
                LOGGER.exception("emergency checkpoint failed")
        raise

    # ensure mandatory glyphs ------------------------------------------------
    if ".notdef" not in model.glyphs:
        model.add_glyph(GlyphModel(name=".notdef", unicode_cp=None, advance=500, status="SYNTHESIZED"))
    if 32 not in model.cmap:
        space_adv = float(metrics.advance_units.get("space", 250.0))
        space_adv = min(max(space_adv, 100.0), 600.0)
        model.add_glyph(
            GlyphModel(name="space", unicode_cp=32, advance=int(round(space_adv)), status="SYNTHESIZED")
        )
    # kerning/features: honest skip (browser milestone)
    model.kerning = {}
    model.features = {}

    checkpoint("reconstruction complete")

    # VALIDATION (heavy route runs ONCE) --------------------------------------
    _emit(ctx, "VALIDATING", {"event": "validation_start", "frozen": len(frozen_dicts)})
    validation_report: Dict[str, Any]
    with tempfile.TemporaryDirectory(prefix="a23font_hv_") as tmp:
        ttf_path = str(Path(tmp) / "probe.ttf")
        otf_path = str(Path(tmp) / "probe.otf")
        build_ttf(model, ttf_path)
        build_otf(model, otf_path)
        heavy = heavy_validation(model, ttf_path, otf_path, vietnamese=False)
        validation_report = heavy.to_dict()
        if not heavy.passed:
            ctx.cache.invalidate(entry_dir, "heavy validation failed")
            return StyleResult(
                ok=False,
                cache_hit=None,
                glyphs_total=len(entries),
                glyphs_frozen=len(frozen_dicts),
                glyphs_failed=len(failed),
                failed_glyphs=sorted(failed),
                ttf=None,
                otf=None,
                validation=validation_report,
                report=_style_report(
                    family, style_name, style_identity, entries, frozen_dicts, failed,
                    pass_counts, budget_exceeded, metrics, lookup.status, _NOTDEF_NOTE, level_log,
                ),
                duration_s=_result_duration(t0),
                error="VALIDATION_FAILED",
            )
        ttf_bytes = Path(ttf_path).read_bytes()
        otf_bytes = Path(otf_path).read_bytes()

    # FINAL ARTIFACTS -----------------------------------------------------------
    ctx.cache.save_fontmodel(entry_dir, model.to_dict())
    ctx.cache.save_final(entry_dir, ttf_bytes, otf_bytes, validation_report)

    report = _style_report(
        family, style_name, style_identity, entries, frozen_dicts, failed,
        pass_counts, budget_exceeded, metrics, lookup.status, _NOTDEF_NOTE, level_log,
    )
    return StyleResult(
        ok=True,
        cache_hit=None,
        glyphs_total=len(entries),
        glyphs_frozen=len(frozen_dicts),
        glyphs_failed=len(failed),
        failed_glyphs=sorted(failed),
        ttf=entry_dir / "final.ttf",
        otf=entry_dir / "final.otf",
        validation=validation_report,
        report=report,
        duration_s=_result_duration(t0),
        error=None,
    )


def _style_report(
    family: str,
    style_name: str,
    identity: str,
    entries: List[dict],
    frozen_dicts: List[dict],
    failed: Dict[str, str],
    pass_counts: Dict[str, int],
    budget_exceeded: bool,
    metrics: MetricsEstimate,
    cache_status_initial: str,
    kerning_note: str,
    level_log: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "family": family,
        "style": style_name,
        "identity": identity,
        "glyphs_total": len(entries),
        "glyphs_frozen": len(frozen_dicts),
        "glyphs_failed": len(failed),
        "failed_glyphs": sorted(failed),
        "failed_reasons": {gid: failed[gid] for gid in sorted(failed)},
        "pass_counts": dict(pass_counts),
        "budget_exceeded": bool(budget_exceeded),
        "refinement_log": level_log,
        "cache_status_initial": cache_status_initial,
        "kerning": kerning_note,
        "features": kerning_note,
        "metrics": {
            "method": metrics.method,
            "ascender_units": float(metrics.ascender_units),
            "descender_units": float(metrics.descender_units),
            "notes": list(metrics.notes),
        },
    }