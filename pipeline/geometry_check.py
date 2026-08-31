"""M3 geometry pass (confidence): candidate glyph model + fast15 cheap checks.

Implements the CHEAP PER-GLYPH CONFIDENCE CHECK stage of fast15.md: finite
coordinates, closed contours, sane bbox/advance, topology counts vs observed
mask, Bezier residual bound, catastrophic self-intersection, degenerate
segments, and normalized 1024<->2048 edge agreement. All failure reasons are
collected and returned (never just the first).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import math

import numpy as np

from pipeline import geometry_core as _core
from pipeline import geometry_fit as _fit

__all__ = ["CandidateGlyph", "ConfidenceReport", "confidence_checks"]


@dataclass
class ConfidenceReport:
    """Outcome of the cheap per-glyph confidence check."""

    passed: bool
    failures: List[str] = field(default_factory=list)
    metrics: Dict[str, object] = field(default_factory=dict)


@dataclass
class CandidateGlyph:
    """One reconstructed glyph candidate in canonical font units.

    contours_units: list of contours; each contour is a list of cubic
    segments ((x,y) x 4). y up, origin at baseline, UPEM=1000.
    """

    gid: str
    unicode: Optional[int]
    contours_units: List[List[Tuple]]
    advance: float
    lsb: float
    bbox: Tuple[float, float, float, float]
    fit_error: float
    report: Optional[ConfidenceReport] = None


# ---------------------------------------------------------------------------
# small vector helpers
# ---------------------------------------------------------------------------

def _all_points(contours) -> np.ndarray:
    pts: List[Tuple[float, float]] = []
    for contour in contours:
        for seg in contour:
            pts.extend(seg)
    if not pts:
        return np.zeros((0, 2), dtype=np.float64)
    return np.asarray(pts, dtype=np.float64)


def _sampled_bbox(contours) -> Tuple[float, float, float, float]:
    chunks = []
    for contour in contours:
        if contour:
            chunks.append(_fit.sample_cubics(contour, samples=6))
    pts = _all_points(contours)
    if len(pts):
        chunks.append(pts)
    if not chunks:
        return (0.0, 0.0, 0.0, 0.0)
    all_pts = np.vstack(chunks)
    return (
        float(all_pts[:, 0].min()),
        float(all_pts[:, 1].min()),
        float(all_pts[:, 0].max()),
        float(all_pts[:, 1].max()),
    )


def _contour_polylines(contours, samples: int = 8) -> List[np.ndarray]:
    return [_fit.sample_cubics(contour, samples=samples) for contour in contours if contour]


def _dilate(mask: np.ndarray, radius: int = 1) -> np.ndarray:
    out = mask.copy()
    for _ in range(radius):
        out = (
            out
            | np.roll(out, 1, axis=0)
            | np.roll(out, -1, axis=0)
            | np.roll(out, 1, axis=1)
            | np.roll(out, -1, axis=1)
        )
    return out


def _edge_band(mask: np.ndarray) -> np.ndarray:
    # radius 2: the band covers the +-2px antialias comparison tolerance;
    # sub-half-pixel grid discretization between the two equivalent-scale
    # renders must not dominate the agreement signal.
    return _dilate(mask, 2) & _dilate(~mask, 2)


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_intersect(a, b, c, d) -> bool:
    """Strict proper intersection (endpoint touches are not counted)."""
    o1 = _orient(a, b, c)
    o2 = _orient(a, b, d)
    o3 = _orient(c, d, a)
    o4 = _orient(c, d, b)
    return (o1 * o2 < -1e-12) and (o3 * o4 < -1e-12)


def _self_intersection_count(contours) -> int:
    """Count proper segment intersections; bbox grid + adjacency skipping."""
    primitives = []  # (contour, seg, sub, a, b)
    for ci, contour in enumerate(contours):
        for si, seg in enumerate(contour):
            pts = _fit._cubic_points(seg, 9)
            for k in range(len(pts) - 1):
                primitives.append((ci, si, k, pts[k], pts[k + 1]))
    n = len(primitives)
    if n < 2:
        return 0

    lengths = [math.hypot(p[4][0] - p[3][0], p[4][1] - p[3][1]) for p in primitives]
    cell = max(1.0, float(np.median(lengths)) * 2.0) if lengths else 1.0

    buckets: Dict[Tuple[int, int], List[int]] = {}
    for i, (_, _, _, a, b) in enumerate(primitives):
        xmin, xmax = sorted((a[0], b[0]))
        ymin, ymax = sorted((a[1], b[1]))
        for cy in range(int(math.floor(ymin / cell)), int(math.floor(ymax / cell)) + 1):
            for cx in range(int(math.floor(xmin / cell)), int(math.floor(xmax / cell)) + 1):
                buckets.setdefault((cx, cy), []).append(i)

    def adjacent(i: int, j: int) -> bool:
        ci, si, ki, _, _ = primitives[i]
        cj, sj, kj, _, _ = primitives[j]
        if ci != cj:
            return False
        if si == sj:
            return abs(ki - kj) <= 1
        nseg = len(contours[ci])
        if abs(si - sj) == 1 or abs(si - sj) == nseg - 1:
            # segments sharing a node: only the touching sub-ends are adjacent
            if si == (sj + 1) % nseg:
                return ki == 0 and kj == 7
            if sj == (si + 1) % nseg:
                return kj == 0 and ki == 7
        return False

    pairs = set()
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        for u in range(len(bucket)):
            for v in range(u + 1, len(bucket)):
                i, j = bucket[u], bucket[v]
                if i > j:
                    i, j = j, i
                pairs.add((i, j))

    count = 0
    for i, j in pairs:
        if adjacent(i, j):
            continue
        _, _, _, a, b = primitives[i]
        _, _, _, c, d = primitives[j]
        # bbox quick reject
        if max(a[0], b[0]) < min(c[0], d[0]) or max(c[0], d[0]) < min(a[0], b[0]):
            continue
        if max(a[1], b[1]) < min(c[1], d[1]) or max(c[1], d[1]) < min(a[1], b[1]):
            continue
        if _segments_intersect(a, b, c, d):
            count += 1
    return count


# ---------------------------------------------------------------------------
# main entry: the cheap per-glyph confidence check
# ---------------------------------------------------------------------------

EDGE_SIZES = (1024, 2048)
EDGE_IOU_MIN = 0.80


def confidence_checks(
    candidate: CandidateGlyph,
    observations_meta: Optional[Dict[str, object]] = None,
) -> ConfidenceReport:
    """Run all fast15 cheap checks; collect every failure reason.

    observations_meta may provide:
      "components"/"holes"    observed mask topology counts (tolerance +-1)
      "source_polylines"      list of (N,2) arrays in font units
      "fit_tol"                fit tolerance in font units for residual bound
    """
    meta = dict(observations_meta or {})
    failures: List[str] = []
    metrics: Dict[str, object] = {}
    contours = candidate.contours_units or []

    # -- finite coordinates -------------------------------------------------
    pts = _all_points(contours)
    finite_ok = bool(np.isfinite(pts).all()) if len(pts) else True
    if not finite_ok:
        failures.append("non_finite")

    # -- closed contours ----------------------------------------------------
    closed_ok = True
    for contour in contours:
        if not contour:
            closed_ok = False
            continue
        start = np.asarray(contour[0][0], dtype=np.float64)
        end = np.asarray(contour[-1][3], dtype=np.float64)
        if not (np.isfinite(start).all() and np.isfinite(end).all()):
            closed_ok = False
            continue
        if float(np.linalg.norm(end - start)) > 1e-6:
            closed_ok = False
    if contours and not closed_ok:
        failures.append("not_closed")

    # -- sane bbox (vertical guard + finite) --------------------------------
    if contours and finite_ok:
        bbox = _sampled_bbox(contours)
        metrics["bbox"] = tuple(round(v, 3) for v in bbox)
        if not all(math.isfinite(v) for v in bbox) or bbox[1] < -250.0 or bbox[3] > 1250.0:
            failures.append("bbox_guard")
    else:
        metrics["bbox"] = tuple(candidate.bbox)

    # -- sane advance --------------------------------------------------------
    adv = float(candidate.advance)
    is_space = candidate.unicode == 32 or candidate.gid == "space"
    if not math.isfinite(adv):
        failures.append("advance_range")
    elif is_space:
        if adv < 100.0:
            failures.append("advance_range")
    elif not (15.0 <= adv <= 2000.0):
        failures.append("advance_range")
    metrics["advance"] = adv

    # -- topology: component & hole counts vs observed mask -----------------
    if contours and finite_ok:
        polys = _contour_polylines(contours)
        n_outer = 0
        n_holes = 0
        for i, poly in enumerate(polys):
            if len(poly) < 3:
                continue
            depth = 0
            probe = poly[0]
            for j, other in enumerate(polys):
                if i != j and len(other) >= 3 and _core._point_in_poly(probe, other):
                    depth += 1
            if depth % 2 == 0:
                n_outer += 1
            else:
                n_holes += 1
        metrics["components"] = n_outer
        metrics["holes"] = n_holes
        if meta.get("components") is not None and abs(n_outer - int(meta["components"])) > 1:
            failures.append("component_count")
        if meta.get("holes") is not None and abs(n_holes - int(meta["holes"])) > 1:
            failures.append("hole_count")
    else:
        metrics["components"] = 0
        metrics["holes"] = 0

    # -- bezier residual vs source polylines --------------------------------
    source_polys = meta.get("source_polylines")
    if source_polys:
        flat_segs = [seg for contour in contours for seg in contour]
        residual = _fit.cubic_residual(source_polys, flat_segs, samples=16)
        fit_tol = float(meta.get("fit_tol", candidate.fit_error if candidate.fit_error > 0 else 1.0))
        metrics["bezier_residual"] = round(float(residual), 4)
        metrics["fit_tol"] = fit_tol
        if residual > 2.0 * fit_tol:
            failures.append("bezier_residual")
    else:
        metrics["bezier_residual"] = None

    # -- degenerate segments --------------------------------------------------
    degenerate = 0
    for contour in contours:
        for seg in contour:
            p0 = np.asarray(seg[0], dtype=np.float64)
            p3 = np.asarray(seg[3], dtype=np.float64)
            controls_finite = all(np.isfinite(np.asarray(p, dtype=np.float64)).all() for p in seg)
            if not controls_finite or float(np.linalg.norm(p3 - p0)) <= 1e-3:
                degenerate += 1
    metrics["degenerate_segments"] = degenerate
    if degenerate:
        failures.append("degenerate_segment")

    # -- catastrophic self-intersection ---------------------------------------
    if contours and finite_ok:
        si_count = _self_intersection_count(contours)
        metrics["self_intersections"] = si_count
        if si_count > 0:
            failures.append("self_intersection")
    else:
        metrics["self_intersections"] = 0

    # -- normalized 1024 <-> 2048 edge agreement ------------------------------
    if contours and finite_ok:
        s1, s2 = EDGE_SIZES
        r1 = _core.rasterize_glyph(contours, candidate.advance, s1, _core.estimate_baseline(s1))
        r2 = _core.rasterize_glyph(contours, candidate.advance, s2, _core.estimate_baseline(s2))
        r2_down = r2.reshape(s1, 2, s1, 2).mean(axis=(1, 3))
        m1 = r1 >= 0.5
        m2 = r2_down >= 0.5
        b1 = _edge_band(m1)
        b2 = _edge_band(m2)
        union = np.count_nonzero(b1 | b2)
        inter = np.count_nonzero(b1 & b2)
        iou = float(inter / union) if union else 1.0
        metrics["edge_agreement"] = round(iou, 4)
        metrics["edge_agreement_sizes"] = list(EDGE_SIZES)
        if iou < EDGE_IOU_MIN:
            failures.append("edge_agreement")
    else:
        metrics["edge_agreement"] = 1.0

    return ConfidenceReport(passed=not failures, failures=failures, metrics=metrics)
