"""M3 geometry pass (fit): closed-polyline simplification + cubic Bezier fit.

Ported and reworked from the author's prior engine (E:/cv/myfonts/engine/
bezier.py, read-only source). The old fitter computed the Schneider A/b
matrices but NEVER solved the 2x2 least-squares system: it silently used
alpha1 = alpha2 = chord_length / 3 for every arc, so control handles were
pure chord-length guesses and the recursive split carried most of the error
budget (overshoot + segment explosion). This module implements the actual
2x2 normal-equation solve with chord/3 fallback only when the system is
degenerate or yields negative alphas, plus one Newton reparameterization
pass and bounded-depth recursive splitting.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

__all__ = ["simplify_closed", "fit_closed_cubic", "sample_cubics", "cubic_residual"]

Point = Tuple[float, float]
CubicSeg = Tuple[Point, Point, Point, Point]


# ---------------------------------------------------------------------------
# Closed Douglas-Peucker simplification
# ---------------------------------------------------------------------------

def _pt_seg_dist(p, a, b) -> float:
    """Distance from point p to segment ab."""
    abx, aby = b[0] - a[0], b[1] - a[1]
    l2 = abx * abx + aby * aby
    if l2 < 1e-12:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / l2
    t = min(max(t, 0.0), 1.0)
    qx, qy = a[0] + t * abx, a[1] + t * aby
    return math.hypot(p[0] - qx, p[1] - qy)


def simplify_closed(pts, tol: float) -> np.ndarray:
    """Douglas-Peucker simplification adapted to a closed polyline.

    Iteratively drops the point whose chord deviation is smallest while it
    stays below `tol`; the loop always keeps at least 4 points. Input is an
    (N,2) array without duplicated closure point; output has the same form.
    """
    pts = np.asarray(pts, dtype=np.float64)
    n = len(pts)
    if n <= 4 or tol <= 0:
        return pts.copy()

    keep = list(range(n))
    changed = True
    while changed and len(keep) > 4:
        changed = False
        m = len(keep)
        worst_i = -1
        worst_d = tol
        for i in range(m):
            prev_k = keep[(i - 1) % m]
            next_k = keep[(i + 1) % m]
            cur_k = keep[i]
            d = _pt_seg_dist(pts[cur_k], pts[prev_k], pts[next_k])
            if d < worst_d:
                worst_d = d
                worst_i = i
        if worst_i >= 0:
            keep.pop(worst_i)
            changed = True
    return pts[np.asarray(keep, dtype=np.int64)].copy()


# ---------------------------------------------------------------------------
# Schneider-style cubic fit for one open arc
# ---------------------------------------------------------------------------

def _chord_params(pts: np.ndarray) -> np.ndarray:
    d = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    total = float(d.sum())
    if total < 1e-12:
        return np.linspace(0.0, 1.0, len(pts))
    t = np.zeros(len(pts))
    t[1:] = np.cumsum(d) / total
    return t


def _basis(t: np.ndarray):
    u = 1.0 - t
    b0 = u ** 3
    b1 = 3.0 * u * u * t
    b2 = 3.0 * u * t * t
    b3 = t ** 3
    return b0, b1, b2, b3


def _solve_alphas(pts, t, p0, p3, t_hat1, t_hat2):
    """2x2 least-squares solve for the two handle lengths alpha1/alpha2.

    Model: Q(t) = (b0+b1) P0 + (b2+b3) P3 + a1 b1 T1 - a2 b2 T2 with
    C1 = P0 + a1 T1 and C2 = P3 - a2 T2 (Schneider, Graphics Gems I).
    The old engine built this same system but never solved it; a correct
    solve needs the (b0+b1)/(b2+b3) baseline and the negated T2 column.
    """
    b0, b1, b2, b3 = _basis(t)
    A1 = b1[:, None] * t_hat1[None, :]
    A2 = -b2[:, None] * t_hat2[None, :]
    baseline = (b0 + b1)[:, None] * p0[None, :] + (b2 + b3)[:, None] * p3[None, :]
    tmp = pts - baseline
    c11 = float(np.einsum("ij,ij->", A1, A1))
    c12 = float(np.einsum("ij,ij->", A1, A2))
    c22 = float(np.einsum("ij,ij->", A2, A2))
    x1 = float(np.einsum("ij,ij->", A1, tmp))
    x2 = float(np.einsum("ij,ij->", A2, tmp))
    det = c11 * c22 - c12 * c12
    chord = float(np.linalg.norm(p3 - p0))
    fallback = max(chord / 3.0, 1e-6)
    if abs(det) < 1e-12:
        return fallback, fallback
    a1 = (x1 * c22 - x2 * c12) / det
    a2 = (c11 * x2 - c12 * x1) / det
    if not (np.isfinite(a1) and np.isfinite(a2)) or a1 < 0.0 or a2 < 0.0:
        return fallback, fallback
    return a1, a2


def _eval_cubic(p0, p1, p2, p3, t):
    b0, b1, b2, b3 = _basis(t)
    return (
        b0[:, None] * p0[None, :]
        + b1[:, None] * p1[None, :]
        + b2[:, None] * p2[None, :]
        + b3[:, None] * p3[None, :]
    )


def _newton_reparam(pts, p0, p1, p2, p3, t):
    """One Newton step minimizing ||B(t) - P||^2 per point."""
    u = 1.0 - t
    b0, b1, b2, b3 = _basis(t)
    q = (
        b0[:, None] * p0[None, :]
        + b1[:, None] * p1[None, :]
        + b2[:, None] * p2[None, :]
        + b3[:, None] * p3[None, :]
    )
    dq = (
        3.0 * (u * u)[:, None] * (p1 - p0)[None, :]
        + 6.0 * (u * t)[:, None] * (p2 - p1)[None, :]
        + 3.0 * (t * t)[:, None] * (p3 - p2)[None, :]
    )
    f = q - pts
    num = np.einsum("ij,ij->i", f, dq)
    den = np.einsum("ij,ij->i", dq, dq)
    with np.errstate(divide="ignore", invalid="ignore"):
        step = np.where(den > 1e-12, num / np.where(den > 1e-12, den, 1.0), 0.0)
    t_new = np.clip(t - step, 0.0, 1.0)
    t_new[0] = 0.0
    t_new[-1] = 1.0
    return t_new


def _fit_arc(
    pts: np.ndarray,
    t_hat1: np.ndarray,
    t_hat2: np.ndarray,
    error_tol: float,
    depth: int,
    max_depth: int,
) -> List[np.ndarray]:
    """Fit one open arc; returns list of (4,2) cubic segments."""
    p0 = pts[0]
    p3 = pts[-1]
    n = len(pts)
    chord = float(np.linalg.norm(p3 - p0))

    if n == 2 or chord < 1e-9:
        p1 = p0 + (p3 - p0) / 3.0
        p2 = p0 + 2.0 * (p3 - p0) / 3.0
        return [np.stack([p0, p1, p2, p3])]

    t = _chord_params(pts)
    a1, a2 = _solve_alphas(pts, t, p0, p3, t_hat1, t_hat2)
    p1 = p0 + a1 * t_hat1
    p2 = p3 - a2 * t_hat2
    # one Newton reparameterization pass, then re-solve alphas
    t = _newton_reparam(pts, p0, p1, p2, p3, t)
    a1, a2 = _solve_alphas(pts, t, p0, p3, t_hat1, t_hat2)
    p1 = p0 + a1 * t_hat1
    p2 = p3 - a2 * t_hat2

    err_pts = _eval_cubic(p0, p1, p2, p3, t)
    dists = np.linalg.norm(err_pts - pts, axis=1)
    split_idx = int(np.argmax(dists))
    max_err = float(dists[split_idx])

    if max_err <= error_tol or depth >= max_depth or n <= 4 or split_idx in (0, n - 1):
        return [np.stack([p0, p1, p2, p3])]

    prev_pt = pts[split_idx - 1]
    next_pt = pts[(split_idx + 1) % n]
    tangent = next_pt - prev_pt
    norm = float(np.linalg.norm(tangent))
    if norm < 1e-12:
        tangent = pts[split_idx + 1] - pts[split_idx - 1]
        norm = float(np.linalg.norm(tangent))
    t_split = tangent / norm if norm > 1e-12 else t_hat1

    left = _fit_arc(pts[: split_idx + 1], t_hat1, t_split, error_tol, depth + 1, max_depth)
    right = _fit_arc(pts[split_idx:], t_split, t_hat2, error_tol, depth + 1, max_depth)
    return left + right


# ---------------------------------------------------------------------------
# Closed-contour driver: corner split + per-arc fit + closure guarantee
# ---------------------------------------------------------------------------

def _turning_angles(pts: np.ndarray) -> np.ndarray:
    """Unsigned turning angle at each vertex of a closed polyline."""
    prev = np.roll(pts, 1, axis=0)
    nxt = np.roll(pts, -1, axis=0)
    v1 = pts - prev
    v2 = nxt - pts
    n1 = np.linalg.norm(v1, axis=1)
    n2 = np.linalg.norm(v2, axis=1)
    ok = (n1 > 1e-9) & (n2 > 1e-9)
    ang = np.zeros(len(pts))
    if ok.any():
        cos_t = np.einsum("ij,ij->i", v1[ok], v2[ok]) / (n1[ok] * n2[ok])
        ang[ok] = np.arccos(np.clip(cos_t, -1.0, 1.0))
    return ang


def _unit(v: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(v))
    if norm < 1e-12:
        return np.array([1.0, 0.0])
    return v / norm


def _corner_free_splits(pts: np.ndarray, error_tol: float) -> List[int]:
    """Evenly spaced split points for corner-free loops.

    Count is chosen from the loop radius estimate so each arc's cubic
    approximation error stays near the tolerance budget; recursion inside
    _fit_arc absorbs any remaining local error.
    """
    n = len(pts)
    perim = float(np.sum(np.linalg.norm(np.diff(np.vstack([pts, pts[:1]]), axis=0), axis=1)))
    r_est = max(perim / (2.0 * math.pi), 1.0)
    phi = (16384.0 * max(error_tol, 1e-3) / r_est) ** 0.25
    k = int(math.ceil(2.0 * math.pi / max(phi, 1e-3)))
    k = max(4, min(k, 32))
    return [(i * n) // k for i in range(k)]


def fit_closed_cubic(
    pts,
    error_tol: float = 1.0,
    corner_angle: float = math.pi / 4.0,
    max_depth: int = 12,
) -> List[CubicSeg]:
    """Fit a closed polyline with cubic Bezier segments.

    Corners are detected by turning angle (> corner_angle) and become arc
    boundaries with sharp tangents; corner-free loops are split at evenly
    spaced points with wrap-around tangents (a single closed arc is
    degenerate for the Schneider solve because start == end). Each arc is
    fit independently: chord-length parameterization, neighbor tangents,
    exact 2x2 least-squares alpha solve, one Newton reparameterization,
    bounded recursive split at the worst point.

    Guarantees: exact endpoint continuity between consecutive segments (the
    split points are shared input vertices) and exact closure (last endpoint
    equals first startpoint).

    Returns a list of ((x,y),(x,y),(x,y),(x,y)) float tuples.
    """
    pts = np.asarray(pts, dtype=np.float64)
    n = len(pts)
    if n < 4:
        return []

    angles = _turning_angles(pts)
    corners = [i for i in range(n) if angles[i] > corner_angle]

    tan_override: Dict[int, np.ndarray] = {}
    if corners:
        splits = corners
    else:
        splits = _corner_free_splits(pts, error_tol)
        for s in splits:  # smooth wrap-around tangents at split points
            tan_override[s] = _unit(pts[(s + 1) % n] - pts[(s - 1) % n])

    arcs: List[Tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    k = len(splits)
    for j in range(k):
        i0 = splits[j]
        i1 = splits[(j + 1) % k]
        take = (i1 - i0) % n
        if take == 0:
            continue
        idx = [(i0 + m) % n for m in range(take + 1)]
        arc_pts = pts[idx]
        t1 = tan_override.get(i0, _unit(arc_pts[1] - arc_pts[0]))
        t2 = tan_override.get(i1, _unit(arc_pts[-1] - arc_pts[-2]))
        arcs.append((arc_pts, t1, t2))

    segments: List[CubicSeg] = []
    for arc_pts, t1, t2 in arcs:
        fitted = _fit_arc(arc_pts, t1, t2, error_tol, 0, max_depth)
        for seg in fitted:
            segments.append(
                (
                    (float(seg[0][0]), float(seg[0][1])),
                    (float(seg[1][0]), float(seg[1][1])),
                    (float(seg[2][0]), float(seg[2][1])),
                    (float(seg[3][0]), float(seg[3][1])),
                )
            )

    if not segments:
        return []

    # enforce exact endpoint continuity + closure despite any float drift
    cleaned: List[CubicSeg] = []
    for seg in segments:
        p0, p1, p2, p3 = seg
        if cleaned:
            p0 = cleaned[-1][3]
        cleaned.append((p0, p1, p2, p3))
    # final closure snap: last endpoint == first startpoint (exact closure)
    first = cleaned[0][0]
    last_seg = cleaned[-1]
    cleaned[-1] = (last_seg[0], last_seg[1], last_seg[2], first)
    return cleaned


# ---------------------------------------------------------------------------
# Sampling + residual helpers (shared with confidence checks and tests)
# ---------------------------------------------------------------------------

def _cubic_points(seg: CubicSeg, samples: int) -> np.ndarray:
    t = np.linspace(0.0, 1.0, samples)
    p0, p1, p2, p3 = (np.asarray(p, dtype=np.float64) for p in seg)
    return _eval_cubic(p0, p1, p2, p3, t)


def _seg_samples(seg: CubicSeg, base: int, max_step: float) -> int:
    """Sample count adapted to segment size (control-polygon length bound)."""
    p0, p1, p2, p3 = (np.asarray(p, dtype=np.float64) for p in seg)
    ctrl_len = float(
        np.linalg.norm(p1 - p0) + np.linalg.norm(p2 - p1) + np.linalg.norm(p3 - p2)
    )
    return max(base, int(math.ceil(ctrl_len / max_step)) + 1)


def sample_cubics(segments: Sequence[CubicSeg], samples: int = 16, max_step: float = 0.5) -> np.ndarray:
    """Densely sample a cubic chain; returns (M,2) points.

    Per-segment sample counts are adapted to segment length so consecutive
    samples are never farther apart than `max_step` (this keeps
    point-to-curve distance queries from being dominated by sample gaps).
    """
    if not segments:
        return np.zeros((0, 2))
    chunks = [_cubic_points(seg, _seg_samples(seg, samples, max_step)) for seg in segments]
    return np.vstack(chunks)


def cubic_residual(polylines: Sequence[np.ndarray], segments: Sequence[CubicSeg], samples: int = 16) -> float:
    """Max distance from polyline vertices to the sampled cubic chain."""
    pts = sample_cubics(segments, samples, max_step=0.5)
    if len(pts) == 0:
        return float("inf")
    worst = 0.0
    for poly in polylines:
        poly = np.asarray(poly, dtype=np.float64)
        if poly.ndim != 2 or len(poly) == 0:
            continue
        d = np.linalg.norm(poly[:, None, :] - pts[None, :, :], axis=2)
        worst = max(worst, float(d.min(axis=1).max()))
    return worst
