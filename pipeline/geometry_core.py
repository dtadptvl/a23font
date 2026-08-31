r"""M3 geometry pass (core): decode, alignment, merge, SDF, contours, rasterize.

Offline correctness module for the fast15.md pipeline (python+numpy+PIL only;
no network). Ported/reworked from the author's prior engine (read-only source
E:\cv\myfonts\engine observation/sdf/topology patterns): one explicit alpha
convention (fg black on white => alpha = 1 - gray/255), one pixel frame
(origin top-left, +y down), real marching squares with sub-pixel
interpolation + saddle disambiguation instead of PIL outline tracing.
"""
from __future__ import annotations

import io
import math
from collections import defaultdict
from typing import List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

__all__ = [
    "decode_raster",
    "estimate_baseline",
    "px_to_units",
    "units_to_px",
    "align_pair",
    "merge_alpha",
    "signed_distance",
    "extract_contours",
    "rasterize_glyph",
]

UPEM_DEFAULT = 1000


# ---------------------------------------------------------------------------
# Raster decode + coordinate frames
# ---------------------------------------------------------------------------

def decode_raster(png_bytes: bytes) -> np.ndarray:
    """Decode PNG bytes to float32 alpha HxW in [0,1].

    Source convention: foreground glyph is BLACK on a WHITE background, so
    ink coverage alpha = 1 - gray/255 (alpha==1.0 means solid ink).
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    gray = np.asarray(img, dtype=np.float32)
    alpha = 1.0 - gray / 255.0
    return np.clip(alpha, 0.0, 1.0, out=alpha)


def estimate_baseline(size_px: int, ascent_frac: float = 0.80) -> float:
    """Baseline y (px from cell top) for an em-cell of `size_px` pixels.

    ascent_frac is the fraction of the em square above the baseline. It is
    overridable so browser-measured metrics can replace the default later.
    Default 0.80 matches the canonical global metrics (ascender 800 /
    descender -200 for UPEM=1000).
    """
    return float(size_px) * float(ascent_frac)


def px_to_units(
    x_px: float,
    y_px: float,
    size_px: int,
    baseline_px: float,
    upem: int = UPEM_DEFAULT,
) -> Tuple[float, float]:
    """Pixel (x right, y down, origin top-left) -> font units (y up, origin baseline)."""
    scale = float(upem) / float(size_px)
    return (float(x_px) * scale, (float(baseline_px) - float(y_px)) * scale)


def units_to_px(
    x_u: float,
    y_u: float,
    size_px: int,
    baseline_px: float,
    upem: int = UPEM_DEFAULT,
) -> Tuple[float, float]:
    """Font units (y up, origin baseline) -> pixel (y down, origin top-left)."""
    scale = float(size_px) / float(upem)
    return (float(x_u) * scale, float(baseline_px) - float(y_u) * scale)


# ---------------------------------------------------------------------------
# Observation alignment / merging
# ---------------------------------------------------------------------------

def _shift_mask(mask: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """Shift a boolean mask by (dx, dy); vacated pixels are False."""
    h, w = mask.shape
    out = np.zeros((h, w), dtype=bool)
    y0, y1 = max(0, -dy), min(h, h - dy)
    x0, x1 = max(0, -dx), min(w, w - dx)
    if y1 > y0 and x1 > x0:
        out[y0 + dy:y1 + dy, x0 + dx:x1 + dx] = mask[y0:y1, x0:x1]
    return out


def align_pair(ref: np.ndarray, tgt: np.ndarray) -> Tuple[int, int, float]:
    """Align `tgt` observation onto `ref` observation.

    Resamples `tgt` to the ref size (PIL bilinear), binarizes both at 0.5,
    seeds with the centroid delta, then local-searches dx,dy in [-4..4]
    around that seed maximizing IoU. Returns (dx, dy, best_iou) where
    (dx, dy) is the shift to apply to tgt.
    """
    ref = np.asarray(ref, dtype=np.float32)
    tgt = np.asarray(tgt, dtype=np.float32)
    if tgt.shape != ref.shape:
        img = Image.fromarray(np.clip(tgt * 255.0, 0, 255).astype(np.uint8))
        img = img.resize((ref.shape[1], ref.shape[0]), Image.BILINEAR)
        tgt = np.asarray(img, dtype=np.float32) / 255.0

    rb = ref >= 0.5
    tb = tgt >= 0.5
    if not rb.any() or not tb.any():
        return (0, 0, 0.0)

    ry, rx = np.argwhere(rb).mean(axis=0)
    ty, tx = np.argwhere(tb).mean(axis=0)
    seed_dx = int(round(rx - tx))
    seed_dy = int(round(ry - ty))

    best_dx, best_dy, best_score = seed_dx, seed_dy, -1.0
    for dy in range(seed_dy - 4, seed_dy + 5):
        for dx in range(seed_dx - 4, seed_dx + 5):
            shifted = _shift_mask(tb, dx, dy)
            inter = np.count_nonzero(rb & shifted)
            union = np.count_nonzero(rb | shifted)
            score = (inter / union) if union else 0.0
            if score > best_score:
                best_dx, best_dy, best_score = dx, dy, score
    return (best_dx, best_dy, float(best_score))


def merge_alpha(
    observations: Sequence[Tuple[np.ndarray, int, int]],
    target_size: Tuple[int, int],
) -> np.ndarray:
    """Average aligned observations into one alpha map.

    Each observation is (alpha, dx, dy): alpha is placed at offset (dx, dy)
    on the target canvas; the result is the per-pixel mean over covering
    observations (0 where none cover).
    """
    h, w = int(target_size[0]), int(target_size[1])
    acc = np.zeros((h, w), dtype=np.float64)
    cnt = np.zeros((h, w), dtype=np.float64)
    for alpha, dx, dy in observations:
        alpha = np.asarray(alpha, dtype=np.float64)
        ah, aw = alpha.shape
        y0, y1 = max(0, dy), min(h, dy + ah)
        x0, x1 = max(0, dx), min(w, dx + aw)
        if y1 <= y0 or x1 <= x0:
            continue
        acc[y0:y1, x0:x1] += alpha[y0 - dy:y1 - dy, x0 - dx:x1 - dx]
        cnt[y0:y1, x0:x1] += 1.0
    out = np.zeros((h, w), dtype=np.float32)
    m = cnt > 0
    out[m] = (acc[m] / cnt[m]).astype(np.float32)
    return out


# ---------------------------------------------------------------------------
# Signed distance field (two-pass chamfer, 3-4 weights)
# ---------------------------------------------------------------------------

def _chamfer_distance(feature: np.ndarray) -> np.ndarray:
    """Distance transform (3-4 chamfer weights) to the nearest True pixel.

    Vectorized two-pass chamfer: within-row sweeps use cumulative minima.
    Result is in weight units (divide by 3 for approximate pixel distance).
    """
    h, w = feature.shape
    inf = float(h + w) * 8.0
    d = np.where(feature, 0.0, inf).astype(np.float64)
    idx3 = 3.0 * np.arange(w, dtype=np.float64)

    # forward pass (top -> bottom, left -> right)
    for i in range(h):
        if i > 0:
            prev = d[i - 1]
            cand = prev + 3.0
            diag_l = np.empty(w)
            diag_l[0] = inf
            diag_l[1:] = prev[:-1] + 4.0
            diag_r = np.empty(w)
            diag_r[-1] = inf
            diag_r[:-1] = prev[1:] + 4.0
            np.minimum(d[i], np.minimum(cand, np.minimum(diag_l, diag_r)), out=d[i])
        np.minimum(d[i], idx3 + np.minimum.accumulate(d[i] - idx3), out=d[i])

    # backward pass (bottom -> top, right -> left)
    for i in range(h - 1, -1, -1):
        if i < h - 1:
            nxt = d[i + 1]
            cand = nxt + 3.0
            diag_l = np.empty(w)
            diag_l[0] = inf
            diag_l[1:] = nxt[:-1] + 4.0
            diag_r = np.empty(w)
            diag_r[-1] = inf
            diag_r[:-1] = nxt[1:] + 4.0
            np.minimum(d[i], np.minimum(cand, np.minimum(diag_l, diag_r)), out=d[i])
        tmp = d[i][::-1] + idx3[::-1]
        d[i] = np.minimum(d[i], np.minimum.accumulate(tmp)[::-1] - idx3)

    return d


def signed_distance(mask_bool: np.ndarray, cap_px: int = 512) -> np.ndarray:
    """Float32 signed distance field of a binary mask.

    Negative inside the glyph, positive outside, units = original pixels.
    Masks larger than cap_px (any side) are downsampled first and the field
    is scaled back to original-resolution pixel distances afterwards
    (metadata scale = cap_px / max_side).
    """
    mask = np.asarray(mask_bool, dtype=bool)
    h, w = mask.shape
    if h == 0 or w == 0:
        return np.zeros((h, w), dtype=np.float32)

    scale = min(1.0, float(cap_px) / float(max(h, w)))
    if scale < 1.0:
        nh = max(1, int(round(h * scale)))
        nw = max(1, int(round(w * scale)))
        img = Image.fromarray(mask.astype(np.uint8) * 255)
        small = np.asarray(img.resize((nw, nh), Image.BILINEAR)) >= 128
    else:
        small = mask

    d_out = _chamfer_distance(small)      # 0 inside, grows outside
    d_in = _chamfer_distance(~small)      # 0 outside, grows inside
    sdf_small = (d_out - d_in) / 3.0      # 3-4 weights -> pixel units

    if scale < 1.0:
        img = Image.fromarray(sdf_small.astype(np.float32), mode="F")
        sdf = np.asarray(img.resize((w, h), Image.BILINEAR), dtype=np.float32)
        sdf = sdf / np.float32(scale)
    else:
        sdf = sdf_small.astype(np.float32)
    return sdf


# ---------------------------------------------------------------------------
# Contour extraction (marching squares + endpoint chaining)
# ---------------------------------------------------------------------------

_MS_TABLE = {
    1: [("L", "T")],
    2: [("T", "R")],
    3: [("L", "R")],
    4: [("R", "B")],
    6: [("T", "B")],
    7: [("L", "B")],
    8: [("B", "L")],
    9: [("T", "B")],
    11: [("R", "B")],
    12: [("R", "L")],
    13: [("T", "R")],
    14: [("L", "T")],
}


def _edge_point(edge, c, r, vtl, vtr, vbr, vbl, level):
    """Sub-pixel crossing point on a cell edge (linear interpolation)."""
    if edge == "T":
        a, b, (x0, y0), (x1, y1) = vtl, vtr, (c, r), (c + 1, r)
    elif edge == "R":
        a, b, (x0, y0), (x1, y1) = vtr, vbr, (c + 1, r), (c + 1, r + 1)
    elif edge == "B":
        a, b, (x0, y0), (x1, y1) = vbl, vbr, (c, r + 1), (c + 1, r + 1)
    else:  # "L"
        a, b, (x0, y0), (x1, y1) = vtl, vbl, (c, r), (c, r + 1)
    dv = b - a
    t = 0.5 if abs(dv) < 1e-12 else (level - a) / dv
    t = min(max(t, 0.0), 1.0)
    return (x0 + t * (x1 - x0), y0 + t * (y1 - y0))


def _signed_area(pts: np.ndarray) -> float:
    x = pts[:, 0]
    y = pts[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _perimeter(pts: np.ndarray) -> float:
    d = np.diff(np.vstack([pts, pts[:1]]), axis=0)
    return float(np.sum(np.hypot(d[:, 0], d[:, 1])))


def _point_in_poly(p, poly: np.ndarray) -> bool:
    """Ray-casting point-in-polygon test."""
    px, py = float(p[0]), float(p[1])
    x = poly[:, 0]
    y = poly[:, 1]
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        yi, yj = y[i], y[j]
        if (yi > py) != (yj > py):
            xi, xj = x[i], x[j]
            if px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def extract_contours(alpha: np.ndarray, level: float = 0.5) -> List[np.ndarray]:
    """Extract closed sub-pixel contours from an alpha map via marching squares.

    Returns a list of (N,2) float arrays in pixel coordinates. Outer loops are
    normalized to positive signed area ("CCW"), holes to negative signed area
    ("CW") in the pixel frame (y down). Loops with |area| < 1 px^2 or
    perimeter < 4 px are dropped. Saddle cells are disambiguated by the cell
    center value; segments are chained through an endpoint map keyed at 1e-3.
    """
    alpha = np.asarray(alpha, dtype=np.float32)
    h, w = alpha.shape
    if h < 2 or w < 2:
        return []

    above = alpha > level
    tl = above[:-1, :-1]
    tr = above[:-1, 1:]
    br = above[1:, 1:]
    bl = above[1:, :-1]
    case = tl.astype(np.int32) | (tr.astype(np.int32) << 1) | (br.astype(np.int32) << 2) | (bl.astype(np.int32) << 3)
    ys, xs = np.nonzero((case != 0) & (case != 15))

    segments: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    for r, c in zip(ys.tolist(), xs.tolist()):
        k = int(case[r, c])
        vtl = float(alpha[r, c])
        vtr = float(alpha[r, c + 1])
        vbr = float(alpha[r + 1, c + 1])
        vbl = float(alpha[r + 1, c])
        if k == 5 or k == 10:
            center = 0.25 * (vtl + vtr + vbr + vbl) > level
            if k == 5:
                pairs = [("T", "R"), ("B", "L")] if center else [("T", "L"), ("R", "B")]
            else:
                pairs = [("T", "L"), ("R", "B")] if center else [("T", "R"), ("B", "L")]
        else:
            pairs = _MS_TABLE[k]
        for e1, e2 in pairs:
            p1 = _edge_point(e1, c, r, vtl, vtr, vbr, vbl, level)
            p2 = _edge_point(e2, c, r, vtl, vtr, vbr, vbl, level)
            if math.hypot(p1[0] - p2[0], p1[1] - p2[1]) > 1e-9:
                segments.append((p1, p2))

    if not segments:
        return []

    # chain segments into closed loops via endpoint map
    def key(p):
        return (round(p[0], 3), round(p[1], 3))

    endpoint_map = defaultdict(list)
    for i, (a, b) in enumerate(segments):
        endpoint_map[key(a)].append((i, 0))
        endpoint_map[key(b)].append((i, 1))

    used = [False] * len(segments)
    raw_loops: List[List[Tuple[float, float]]] = []
    for i in range(len(segments)):
        if used[i]:
            continue
        used[i] = True
        a, b = segments[i]
        loop = [a, b]
        start_key = key(a)
        cur_key = key(b)
        while cur_key != start_key:
            nxt = None
            for j, which in endpoint_map[cur_key]:
                if not used[j]:
                    nxt = (j, which)
                    break
            if nxt is None:
                break
            j, which = nxt
            used[j] = True
            c_pt, d_pt = segments[j]
            nxt_pt = d_pt if which == 0 else c_pt
            loop.append(nxt_pt)
            cur_key = key(nxt_pt)
        if cur_key == start_key and len(loop) >= 4:
            raw_loops.append(loop[:-1])  # drop duplicate of the start point

    # filter + classify (outer vs hole by nesting depth) + normalize winding
    loops: List[np.ndarray] = []
    for lp in raw_loops:
        arr = np.asarray(lp, dtype=np.float64)
        if abs(_signed_area(arr)) < 1.0 or _perimeter(arr) < 4.0:
            continue
        loops.append(arr)

    result: List[np.ndarray] = []
    for i, arr in enumerate(loops):
        probe = arr[0]
        depth = 0
        for j, other in enumerate(loops):
            if i != j and _point_in_poly(probe, other):
                depth += 1
        area = _signed_area(arr)
        if depth % 2 == 0:  # outer loop: positive signed area
            if area < 0:
                arr = arr[::-1].copy()
        else:               # hole: negative signed area
            if area > 0:
                arr = arr[::-1].copy()
        result.append(arr)
    return result


# ---------------------------------------------------------------------------
# Vector rasterization of cubic outlines (adaptive flatten + scanline fill)
# ---------------------------------------------------------------------------

def _mid(p, q):
    return ((p[0] + q[0]) * 0.5, (p[1] + q[1]) * 0.5)


def _point_line_dist(p, a, b) -> float:
    bx, by = b[0] - a[0], b[1] - a[1]
    l2 = bx * bx + by * by
    if l2 < 1e-12:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    cross = abs(bx * (a[1] - p[1]) - by * (a[0] - p[0]))
    return cross / math.sqrt(l2)


def _flatten_cubic(p0, p1, p2, p3, tol: float, depth: int = 0) -> List[Tuple[float, float]]:
    """Flatten one cubic to polyline points AFTER p0 (p3 included).

    Recursive subdivision; stops when both control points are within `tol`
    px of the chord (max deviation criterion) or at bounded depth.
    """
    if depth >= 20:
        return [p3]
    d1 = _point_line_dist(p1, p0, p3)
    d2 = _point_line_dist(p2, p0, p3)
    if max(d1, d2) <= tol:
        return [p3]
    m01 = _mid(p0, p1)
    m12 = _mid(p1, p2)
    m23 = _mid(p2, p3)
    m012 = _mid(m01, m12)
    m123 = _mid(m12, m23)
    m0123 = _mid(m012, m123)
    return _flatten_cubic(p0, m01, m012, m0123, tol, depth + 1) + _flatten_cubic(
        m0123, m123, m23, p3, tol, depth + 1
    )


def rasterize_glyph(
    contours_units: Sequence[Sequence],
    advance_units: float,
    size_px: int,
    baseline_px: float,
    upem: int = UPEM_DEFAULT,
) -> np.ndarray:
    """Rasterize cubic contours (font units) to a size_px x size_px alpha cell.

    Cubics are flattened by adaptive subdivision (max deviation 0.25 px) and
    filled with an even-odd scanline rule; per row the fill mask is computed
    vectorized via np.searchsorted over the sorted x intersections.
    `advance_units` is accepted for API compatibility (the glyph cell is one
    em square; advance affects placement, not the cell).
    """
    h = w = int(size_px)
    alpha = np.zeros((h, w), dtype=np.float32)
    if not contours_units:
        return alpha

    polys: List[np.ndarray] = []
    for contour in contours_units:
        segs = list(contour)
        if not segs:
            continue
        pts: List[Tuple[float, float]] = [units_to_px(*segs[0][0], size_px, baseline_px, upem)]
        for seg in segs:
            q0, q1, q2, q3 = (units_to_px(*p, size_px, baseline_px, upem) for p in seg)
            pts.extend(_flatten_cubic(q0, q1, q2, q3, 0.25))
        if len(pts) >= 3:
            polys.append(np.asarray(pts, dtype=np.float64))
    if not polys:
        return alpha

    # gather scanline intersections per row
    xs_per_row: List[List[float]] = [[] for _ in range(h)]
    for poly in polys:
        n = len(poly)
        x = poly[:, 0]
        y = poly[:, 1]
        x1 = np.roll(x, -1)
        y1 = np.roll(y, -1)
        for i in range(n):
            ya, yb = y[i], y1[i]
            dy = yb - ya
            if abs(dy) < 1e-9:
                continue
            lo, hi = (ya, yb) if ya < yb else (yb, ya)
            r0 = int(math.ceil(lo - 0.5))
            r1 = int(math.ceil(hi - 0.5))  # half-open: rows where lo <= r+.5 < hi
            r0 = max(0, r0)
            r1 = min(h, r1)
            if r1 <= r0:
                continue
            rows = np.arange(r0, r1, dtype=np.float64) + 0.5
            xs = x[i] + (rows - ya) * (x1[i] - x[i]) / dy
            bucket = xs_per_row
            for rr, xx in zip(range(r0, r1), xs.tolist()):
                bucket[rr].append(xx)

    xcoords = np.arange(w, dtype=np.float64) + 0.5  # pixel centers (matches row centers)
    for r in range(h):
        xs = xs_per_row[r]
        if not xs:
            continue
        xs_arr = np.sort(np.asarray(xs, dtype=np.float64))
        idx = np.searchsorted(xs_arr, xcoords, side="right")
        alpha[r, :] = (idx % 2 == 1)
    return alpha
