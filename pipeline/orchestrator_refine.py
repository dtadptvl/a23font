"""Reconstruction helpers for pipeline.orchestrator (fast15.md, M5).

Pure/offline building blocks used by the stage machine:
  * the fast geometry pass (align -> merge -> contours -> simplify -> cubic
    fit -> font units via the metrics-derived baseline),
  * the refinement ladder observation spec (fast15 levels 1..4),
  * the bounded local optimizer (<=40 iterations, coordinate descent on
    interior cubic control points + advance within +/-8%, band error at 512
    against the merged observation).

Alignment searches run at reduced resolution (max side ALIGN_SEARCH_MAX_PX)
and the integer shift is scaled back; this keeps 2048/4096 merges cheap
without weakening any confidence threshold.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from pipeline import geometry_core as core
from pipeline import geometry_fit as fit
from pipeline.geometry_check import CandidateGlyph, ConfidenceReport, confidence_checks

__all__ = [
    "REFINEMENT_LEVELS",
    "ReconstructionPack",
    "OptimizationResult",
    "resample_to",
    "align_and_merge",
    "reconstruct_candidate",
    "run_confidence",
    "local_optimizer",
]

# fast15 refinement ladder: level -> NEW (size_px, x_phase) observations.
# Only missing observations are acquired; earlier ones are always reused.
REFINEMENT_LEVELS: Tuple[Tuple[int, Tuple[Tuple[int, float], ...]], ...] = (
    (1, ((1024, 0.5), (2048, 0.5))),
    (2, ((512, 0.0), (512, 0.5))),
    (3, ((4096, 0.0), (4096, 0.5))),
    (4, ((2048, 0.25), (2048, 0.75), (4096, 0.25), (4096, 0.75))),
)

ALIGN_SEARCH_MAX_PX = 512
OPT_SIZE_PX = 512
OPT_MAX_ITERS = 40
OPT_ADVANCE_FRACTION = 0.08
# simplify_closed removes one point per pass (quadratic); bound its input.
# The bezier-residual confidence check still gates geometric fidelity.
MAX_FIT_INPUT_POINTS = 800
# cubic_residual materializes one (poly x samples) distance matrix per
# contour; cap each observed source polyline to bound peak memory while
# keeping sub-4px evidence spacing at 2048.
SOURCE_POLY_MAX_POINTS = 1200


@dataclass
class ReconstructionPack:
    """One glyph geometry-pass result (candidate + evidence + merged alpha)."""

    candidate: CandidateGlyph
    observations_meta: Dict[str, object]
    merged_alpha: np.ndarray
    ref_height: int
    ref_width: int


@dataclass
class OptimizationResult:
    contours_units: List[List[Tuple]]
    advance: float
    band_error: float
    iterations: int


def resample_to(alpha: np.ndarray, shape: Tuple[int, int]) -> np.ndarray:
    """Bilinear resample of an alpha map to (h, w)."""
    h, w = int(shape[0]), int(shape[1])
    alpha = np.asarray(alpha, dtype=np.float32)
    if alpha.shape == (h, w):
        return alpha
    img = Image.fromarray(np.clip(alpha * 255.0, 0, 255).astype(np.uint8))
    img = img.resize((w, h), Image.BILINEAR)
    return np.asarray(img, dtype=np.float32) / 255.0


def _align_shift(ref: np.ndarray, tgt: np.ndarray) -> Tuple[int, int]:
    """Integer (dx, dy) aligning tgt onto ref, searched at reduced resolution."""
    h, w = ref.shape
    longest = max(h, w)
    if longest <= ALIGN_SEARCH_MAX_PX:
        dx, dy, _ = core.align_pair(ref, tgt)
        return int(dx), int(dy)
    scale = ALIGN_SEARCH_MAX_PX / float(longest)
    small_shape = (max(1, int(round(h * scale))), max(1, int(round(w * scale))))
    ref_s = resample_to(ref, small_shape)
    tgt_s = resample_to(tgt, small_shape)
    dx_s, dy_s, _ = core.align_pair(ref_s, tgt_s)
    return int(round(dx_s / scale)), int(round(dy_s / scale))


def align_and_merge(
    alphas: Sequence[np.ndarray],
) -> Tuple[Optional[np.ndarray], Tuple[int, int]]:
    """Align every observation onto the largest one and average them.

    Returns (merged, ref_shape); (None, (0, 0)) when nothing is usable.
    """
    usable = [np.asarray(a, dtype=np.float32) for a in alphas if a is not None]
    usable = [a for a in usable if a.size and float(a.max()) > 0.0]
    if not usable:
        return None, (0, 0)
    ref_idx = max(range(len(usable)), key=lambda i: usable[i].shape[0] * usable[i].shape[1])
    ref = usable[ref_idx]
    placed: List[Tuple[np.ndarray, int, int]] = [(ref, 0, 0)]
    for i, alpha in enumerate(usable):
        if i == ref_idx:
            continue
        if alpha.shape != ref.shape:
            alpha = resample_to(alpha, ref.shape)
        dx, dy = _align_shift(ref, alpha)
        placed.append((alpha, dx, dy))
    merged = core.merge_alpha(placed, ref.shape)
    return merged, ref.shape


def _polyline_deviation(src: np.ndarray, dst: np.ndarray, densify: int = 4) -> float:
    """Max distance from src vertices to the (densified) dst polyline."""
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    if len(src) == 0 or len(dst) < 2:
        return float("inf")
    dense = [dst]
    for k in range(1, int(densify)):
        w = k / float(densify)
        dense.append(dst[:-1] * (1.0 - w) + dst[1:] * w)
    dense.append(np.roll(dst, -1, axis=0)[:-1])
    pts = np.vstack(dense)
    d = np.linalg.norm(src[:, None, :] - pts[None, :, :], axis=2)
    return float(d.min(axis=1).max())


def _corner_angles(pts: np.ndarray, window_px: float = 4.0) -> np.ndarray:
    """Windowed turning angle at every vertex of a closed polyline.

    Directions are taken over a +/-window_px arc window, so 1px marching-
    squares staircase steps average out to the edge direction while real
    corners (persistent over several pixels) keep their large angle.
    """
    n = len(pts)
    if n < 6:
        return np.zeros(n)
    closed = np.vstack([pts, pts[:1]])
    step = np.linalg.norm(np.diff(closed, axis=0), axis=1)
    med = float(np.median(step)) if len(step) else 1.0
    w = max(2, int(round(window_px / max(med, 1e-6))))
    w = min(w, n // 3)
    idx = np.arange(n)
    v1 = pts - pts[(idx - w) % n]
    v2 = pts[(idx + w) % n] - pts
    n1 = np.linalg.norm(v1, axis=1)
    n2 = np.linalg.norm(v2, axis=1)
    ang = np.zeros(n)
    ok = (n1 > 1e-9) & (n2 > 1e-9)
    if ok.any():
        cos_t = np.einsum("ij,ij->i", v1[ok], v2[ok]) / (n1[ok] * n2[ok])
        ang[ok] = np.arccos(np.clip(cos_t, -1.0, 1.0))
    return ang


def _corner_indices(
    pts: np.ndarray, thresh: float = math.pi / 6.0, min_gap_px: float = 8.0
) -> np.ndarray:
    """Windowed-angle corner vertices, de-clustered.

    Two suppression passes: +/-3 vertex non-maximum suppression, then a
    cyclic merge of corners closer than min_gap_px of arc length (staircase
    corner clusters collapse to their strongest vertex; 1px-spaced anchors
    would poison the cubic fit with zigzag arcs and self-intersections).
    """
    ang = _corner_angles(pts)
    cand = np.nonzero(ang > thresh)[0]
    if len(cand) == 0:
        return cand
    n = len(pts)
    keep = []
    for i in cand:
        window = ang[[(i + d) % n for d in range(-3, 4)]]
        if ang[i] >= float(window.max()) - 1e-12:
            keep.append(int(i))
    if len(keep) <= 1:
        return np.asarray(keep, dtype=np.int64)
    closed = np.vstack([pts, pts[:1]])
    cum = np.concatenate(
        [[0.0], np.cumsum(np.linalg.norm(np.diff(closed, axis=0), axis=1))]
    )
    total = float(cum[-1])
    keep.sort()
    positions = [cum[i] for i in keep]
    # cyclic grouping of corners closer than min_gap_px
    groups: list = []
    current = [0]
    for k in range(1, len(keep)):
        if positions[k] - positions[k - 1] < min_gap_px:
            current.append(k)
        else:
            groups.append(current)
            current = [k]
    groups.append(current)
    if len(groups) > 1 and (total - positions[-1] + positions[0]) < min_gap_px:
        groups[0] = groups[-1] + groups[0]
        groups.pop()
    merged = [max(g, key=lambda k: float(ang[keep[k]])) for g in groups]
    return np.asarray(sorted(keep[m] for m in merged), dtype=np.int64)


def _smooth_staircase(
    loop: np.ndarray, corners: np.ndarray, passes: int = 3
) -> np.ndarray:
    """Corner-aware local averaging of a closed polyline.

    Hard-edged rasters produce ~1px staircase boundary runs; repeated box
    averaging converges those runs toward the true edge while corner
    vertices stay fixed, so real corners are never blurred away.
    """
    pts = np.asarray(loop, dtype=np.float64).copy()
    n = len(pts)
    if n < 8:
        return pts
    fixed = np.zeros(n, dtype=bool)
    if len(corners):
        fixed[np.asarray(corners, dtype=np.int64) % n] = True
    idx = np.arange(n)
    movable = ~fixed
    for _ in range(int(passes)):
        if not movable.any():
            break
        averaged = (pts[(idx - 1) % n] + pts[idx] + pts[(idx + 1) % n]) / 3.0
        pts[movable] = averaged[movable]
    return pts


def _interp_at_lengths(
    closed: np.ndarray, cum: np.ndarray, total: float, targets: np.ndarray
) -> np.ndarray:
    """Interpolate closed-polyline points at arc lengths (mod total)."""
    n = len(closed) - 1
    t = np.mod(targets, total)
    idx = np.clip(np.searchsorted(cum, t, side="right") - 1, 0, n - 1)
    seg_len = cum[idx + 1] - cum[idx]
    safe = np.where(seg_len > 1e-12, seg_len, 1.0)
    w = np.clip((t - cum[idx]) / safe, 0.0, 1.0)
    return closed[idx] + (closed[idx + 1] - closed[idx]) * w[:, None]


def _resample_loop(loop: np.ndarray, max_points: int, corners=None) -> np.ndarray:
    """Arc-length resample of a closed polyline that PRESERVES sharp corners.

    Uniform decimation clips corners between samples and shows up later as a
    bezier-residual failure, so corner vertices stay exactly; the remaining
    budget is distributed along the corner-to-corner intervals proportionally
    to their arc length. Corner-free loops fall back to uniform spacing.
    """
    pts = np.asarray(loop, dtype=np.float64)
    n = len(pts)
    if n <= max_points:
        return pts
    closed = np.vstack([pts, pts[:1]])
    seg = np.linalg.norm(np.diff(closed, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(cum[-1])
    if total <= 0:
        return pts

    if corners is None:
        corners = _corner_indices(pts)
    corners = np.asarray(corners, dtype=np.int64)
    if len(corners) == 0 or len(corners) >= max_points:
        targets = np.linspace(0.0, total, int(max_points), endpoint=False)
        return _interp_at_lengths(closed, cum, total, targets)

    anchors = np.sort(corners)
    k = len(anchors)
    budget = int(max_points) - k
    lengths = np.empty(k)
    for j in range(k):
        i0, i1 = anchors[j], anchors[(j + 1) % k]
        lengths[j] = (cum[i1] - cum[i0]) if i1 > i0 else (total - cum[i0] + cum[i1])
    lengths = np.maximum(lengths, 0.0)
    length_sum = float(lengths.sum())
    if budget <= 0 or length_sum <= 0:
        return pts[anchors]
    alloc = np.floor(budget * lengths / length_sum).astype(int)
    remainder = budget - int(alloc.sum())
    if remainder > 0:
        frac = budget * lengths / length_sum - alloc
        alloc[np.argsort(-frac, kind="stable")[:remainder]] += 1

    out = []
    starts = cum[anchors]
    for j in range(k):
        out.append(pts[anchors[j]][None, :])
        m = int(alloc[j])
        if m > 0 and lengths[j] > 0:
            targets = starts[j] + lengths[j] * (np.arange(1, m + 1) / (m + 1.0))
            out.append(_interp_at_lengths(closed, cum, total, targets))
    return np.vstack(out)


def _loop_signed_area(loop: np.ndarray) -> float:
    x = loop[:, 0]
    y = loop[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def reconstruct_candidate(
    gid: str,
    cp: int,
    observations: Dict[Tuple[int, float, float], object],
    metrics: object,
    upem: int = 1000,
) -> Optional[ReconstructionPack]:
    """Fast geometry pass over all acquired observations of one glyph.

    observations maps (size_px, x_phase, y_phase) -> Observation-like object
    with an .alpha attribute. Returns None when no contour is extractable.
    """
    alphas = [
        obs.alpha
        for (_size, _xp, _yp), obs in sorted(observations.items())
        if obs is not None and getattr(obs, "alpha", None) is not None
    ]
    merged, shape = align_and_merge(alphas)
    if merged is None:
        return None
    h, w = shape
    loops_px = core.extract_contours(merged, 0.5)
    if not loops_px:
        return None

    ascent_frac = float(getattr(metrics, "ascender_units", 800.0)) / float(upem)
    ascent_frac = min(max(ascent_frac, 0.5), 1.0)
    baseline = float(h) * ascent_frac

    components = sum(1 for lp in loops_px if _loop_signed_area(lp) > 0)
    holes = sum(1 for lp in loops_px if _loop_signed_area(lp) < 0)

    fit_tol_px = 1.0
    fit_tol_units = fit_tol_px * float(upem) / float(h)

    contours_units: List[List[Tuple]] = []
    source_polylines: List[np.ndarray] = []
    for loop in loops_px:
        # smoothing fixed points come from the raw loop; the RESAMPLE anchors
        # are re-detected after smoothing so staircase corner clusters collapse
        # to single true corners (1px-spaced anchors would poison the fit).
        corners = _corner_indices(loop)
        loop = _smooth_staircase(loop, corners)
        corners = _corner_indices(loop)
        bounded = _resample_loop(loop, MAX_FIT_INPUT_POINTS, corners=corners)
        simple = fit.simplify_closed(bounded, tol=0.5)
        # simplify_closed removes one point per pass; on staircase boundaries
        # the greedy scheme can drift far beyond tol. Verify the result and
        # fall back to the bounded polyline when simplification broke shape.
        if _polyline_deviation(bounded, simple) > max(2.0, 2.0 * fit_tol_px):
            simple = bounded
        segs_px = fit.fit_closed_cubic(simple, error_tol=fit_tol_px)
        if not segs_px:
            continue
        contour = [
            tuple(core.px_to_units(p[0], p[1], h, baseline, upem) for p in seg)
            for seg in segs_px
        ]
        contours_units.append(contour)
        evidence = _resample_loop(loop, SOURCE_POLY_MAX_POINTS, corners=corners)
        units = np.empty_like(evidence, dtype=np.float64)
        units[:, 0] = evidence[:, 0] * float(upem) / float(h)
        units[:, 1] = (baseline - evidence[:, 1]) * float(upem) / float(h)
        source_polylines.append(units)
    if not contours_units:
        return None

    advance = float(getattr(metrics, "advance_units", {}).get(gid, 500.0))
    advance = min(max(advance, 15.0), 2000.0)

    xs: List[float] = []
    ys: List[float] = []
    for contour in contours_units:
        for seg in contour:
            for p in seg:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
    bbox = (min(xs), min(ys), max(xs), max(ys))

    candidate = CandidateGlyph(
        gid=gid,
        unicode=cp if cp > 0 else None,
        contours_units=contours_units,
        advance=advance,
        lsb=bbox[0],
        bbox=bbox,
        fit_error=fit_tol_units,
    )
    observations_meta: Dict[str, object] = {
        "components": components,
        "holes": holes,
        "source_polylines": source_polylines,
        "fit_tol": fit_tol_units,
    }
    return ReconstructionPack(candidate, observations_meta, merged, h, w)


def run_confidence(pack: ReconstructionPack) -> ConfidenceReport:
    """Cheap per-glyph confidence check with observed topology counts."""
    return confidence_checks(pack.candidate, dict(pack.observations_meta))


# ---------------------------------------------------------------------------
# local optimizer (fast15 LOCAL OPTIMIZER, failing glyph only)
# ---------------------------------------------------------------------------

def _dilate2(mask: np.ndarray) -> np.ndarray:
    out = mask.copy()
    for _ in range(2):
        out = (
            out
            | np.roll(out, 1, axis=0)
            | np.roll(out, -1, axis=0)
            | np.roll(out, 1, axis=1)
            | np.roll(out, -1, axis=1)
        )
    return out


def _edge_band(mask: np.ndarray) -> np.ndarray:
    # radius-2 band around the mask edge (matches geometry_check semantics)
    return _dilate2(mask) & _dilate2(~mask)


@dataclass
class _MutableModel:
    contours: List[List[List[List[float]]]]  # contour > seg > point > [x, y]
    advance: float


def _snapshot(model: _MutableModel) -> Tuple[List[List[Tuple]], float]:
    contours = [
        [[(float(p[0]), float(p[1])) for p in seg] for seg in contour]
        for contour in model.contours
    ]
    return contours, float(model.advance)


def local_optimizer(
    pack: ReconstructionPack,
    metrics: object,
    upem: int = 1000,
    size_px: int = OPT_SIZE_PX,
    max_iters: int = OPT_MAX_ITERS,
) -> Optional[OptimizationResult]:
    """Bounded coordinate descent on interior control points + advance.

    Objective: edge-band disagreement between the candidate rasterized at
    size_px and the merged observation resampled to the same cell. Only
    interior control points (p1, p2) move, so contour closure and segment
    continuity hold by construction; advance is nudged within +/-8%.
    Returns None when the observation cell is not square (the optimizer
    target assumes an em-square cell).
    """
    merged = pack.merged_alpha
    if merged.shape[0] != merged.shape[1]:
        return None
    candidate = pack.candidate

    ascent_frac = float(getattr(metrics, "ascender_units", 800.0)) / float(upem)
    ascent_frac = min(max(ascent_frac, 0.5), 1.0)
    baseline = core.estimate_baseline(size_px, ascent_frac)

    target = resample_to(merged, (size_px, size_px))
    tmask = target >= 0.5
    if not tmask.any():
        return None
    band = _edge_band(tmask)
    band_n = int(np.count_nonzero(band))
    if band_n == 0:
        return None

    def band_error(contours, advance: float) -> float:
        rast = core.rasterize_glyph(contours, advance, size_px, baseline, upem)
        diff = (rast >= 0.5) != tmask
        return float(np.count_nonzero(diff & band)) / float(band_n)

    base_advance = float(candidate.advance)
    model = _MutableModel(
        contours=[
            [[[float(p[0]), float(p[1])] for p in seg] for seg in contour]
            for contour in candidate.contours_units
        ],
        advance=base_advance,
    )
    best_err = band_error(*_snapshot(model))
    best_contours, best_advance = _snapshot(model)
    iters = 1

    for delta in (4.0, 2.0, 1.0, 0.5):
        if iters >= max_iters:
            break
        improved = True
        while improved and iters < max_iters:
            improved = False
            for contour in model.contours:
                for seg in contour:
                    for pi in (1, 2):  # interior control points only
                        for ci in (0, 1):
                            if iters >= max_iters:
                                break
                            old = seg[pi][ci]
                            accepted = False
                            for sign in (1.0, -1.0):
                                seg[pi][ci] = old + sign * delta
                                err = band_error(*_snapshot(model))
                                iters += 1
                                if err < best_err - 1e-9:
                                    best_err = err
                                    best_contours, best_advance = _snapshot(model)
                                    accepted = True
                                    improved = True
                                    break
                            if not accepted:
                                seg[pi][ci] = old

    # advance nudge within +/-8% (evaluated around the best geometry)
    for factor in (1.02, 0.98, 1.05, 0.95, 1.08, 0.92):
        if iters >= max_iters:
            break
        cand_advance = base_advance * factor
        if abs(cand_advance - base_advance) > OPT_ADVANCE_FRACTION * base_advance + 1e-9:
            continue
        err = band_error(best_contours, cand_advance)
        iters += 1
        if err < best_err - 1e-9:
            best_err = err
            best_advance = cand_advance

    return OptimizationResult(
        contours_units=best_contours,
        advance=best_advance,
        band_error=best_err,
        iterations=iters,
    )