"""M3.A1 geometry pass tests: decode, contours, fit, rasterize, confidence.

All offline; fixtures are generated in-process via PIL (see
tests/geometry_fixtures.py). No network, no live endpoints.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from pipeline import geometry as g
from pipeline.geometry_core import _perimeter, _signed_area
from tests import geometry_fixtures as fx


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def circle_alpha():
    return g.decode_raster(fx.circle_png())


@pytest.fixture(scope="module")
def ring_alpha():
    return g.decode_raster(fx.ring_png())


@pytest.fixture(scope="module")
def circle_loop(circle_alpha):
    loops = g.extract_contours(circle_alpha, 0.5)
    assert len(loops) == 1
    return loops[0]


@pytest.fixture(scope="module")
def circle_fit(circle_loop):
    return g.fit_closed_cubic(circle_loop, error_tol=1.0)


def _to_units_contour(segs, size_px, baseline):
    return [tuple(g.px_to_units(p[0], p[1], size_px, baseline) for p in seg) for seg in segs]


# ---------------------------------------------------------------------------
# decode sanity
# ---------------------------------------------------------------------------

def test_decode_raster_shape_and_convention(circle_alpha):
    assert circle_alpha.shape == (1024, 1024)
    assert circle_alpha.dtype == np.float32
    # fg black on white -> alpha 1 inside the circle, 0 outside
    assert circle_alpha[512, 512] > 0.99
    assert circle_alpha[0, 0] < 0.01
    assert float(circle_alpha.min()) >= 0.0
    assert float(circle_alpha.max()) <= 1.0


def test_estimate_baseline_and_units_roundtrip():
    bl = g.estimate_baseline(1024)
    assert bl == pytest.approx(819.2)
    x_u, y_u = g.px_to_units(512.0, 819.2, 1024, bl)
    assert x_u == pytest.approx(500.0)
    assert y_u == pytest.approx(0.0)
    x_px, y_px = g.units_to_px(x_u, y_u, 1024, bl)
    assert x_px == pytest.approx(512.0)
    assert y_px == pytest.approx(819.2)


def test_align_pair_recovers_shift(circle_alpha):
    tgt = np.zeros_like(circle_alpha)
    tgt[:, 3:] = circle_alpha[:, :-3]  # tgt ink shifted 3 px right vs ref
    dx, dy, score = g.align_pair(circle_alpha, tgt)
    # convention: (dx, dy) is the shift to APPLY to tgt to align it onto ref
    assert dx == -3
    assert dy == 0
    assert score > 0.95


def test_merge_alpha_averages(circle_alpha):
    merged = g.merge_alpha([(circle_alpha, 0, 0), (circle_alpha, 0, 0)], (1024, 1024))
    assert merged.shape == (1024, 1024)
    assert merged[512, 512] == pytest.approx(circle_alpha[512, 512], abs=1e-6)


def test_signed_distance_signs(circle_alpha):
    sdf = g.signed_distance(circle_alpha >= 0.5, cap_px=512)
    assert sdf.shape == (1024, 1024)
    assert sdf.dtype == np.float32
    assert sdf[512, 512] < 0.0  # inside -> negative
    assert sdf[0, 0] > 0.0      # outside -> positive


# ---------------------------------------------------------------------------
# contour extraction
# ---------------------------------------------------------------------------

def test_extract_contours_circle(circle_loop):
    perim = _perimeter(circle_loop)
    expected = 2 * math.pi * 300
    assert abs(perim - expected) / expected <= 0.05
    assert _signed_area(circle_loop) > 0  # outer loop: positive signed area


def test_extract_contours_ring(ring_alpha):
    loops = g.extract_contours(ring_alpha, 0.5)
    assert len(loops) == 2
    areas = [_signed_area(lp) for lp in loops]
    positives = [a for a in areas if a > 0]
    negatives = [a for a in areas if a < 0]
    assert len(positives) == 1 and len(negatives) == 1  # one outer + one hole
    # hole area ~ pi * (150^2), outer ~ pi * (300^2)
    assert positives[0] == pytest.approx(math.pi * 300**2, rel=0.03)
    assert abs(negatives[0]) == pytest.approx(math.pi * 150**2, rel=0.03)


def test_extract_contours_corner_shapes():
    for png, expected_loops in [
        (fx.rounded_square_png(), 1),
        (fx.triangle_png(), 1),
        (fx.letter_l_png(), 1),
        (fx.letter_h_png(), 1),
    ]:
        alpha = g.decode_raster(png)
        loops = g.extract_contours(alpha, 0.5)
        assert len(loops) == expected_loops
        assert _signed_area(loops[0]) > 0


def test_extract_contours_drops_noise():
    alpha = np.zeros((64, 64), dtype=np.float32)
    alpha[30:32, 30:32] = 1.0  # 2x2 px speck: area ~4 px^2 but perimeter 8 px
    alpha[10, 10] = 1.0        # single pixel: below both thresholds
    loops = g.extract_contours(alpha, 0.5)
    # only loops passing both filters survive; single px must be gone
    for lp in loops:
        assert abs(_signed_area(lp)) >= 1.0
        assert _perimeter(lp) >= 4.0


# ---------------------------------------------------------------------------
# cubic fitting
# ---------------------------------------------------------------------------

def test_fit_circle_segments_finite_and_closed(circle_fit):
    assert len(circle_fit) >= 4
    for seg in circle_fit:
        for pt in seg:
            assert math.isfinite(pt[0]) and math.isfinite(pt[1])
    # exact endpoint continuity + exact closure
    for i in range(len(circle_fit) - 1):
        assert circle_fit[i][3] == circle_fit[i + 1][0]
    assert circle_fit[-1][3] == circle_fit[0][0]


def test_fit_circle_residual(circle_loop, circle_fit):
    residual = g.cubic_residual([circle_loop], circle_fit, samples=16)
    assert residual <= 1.5  # px at 1024


def test_fit_letter_l_detects_corners():
    alpha = g.decode_raster(fx.letter_l_png())
    loop = g.extract_contours(alpha, 0.5)[0]
    segs = g.fit_closed_cubic(loop, error_tol=1.0)
    # an L has 6 corners; the fit needs at least 6 segments (>= corner count)
    assert len(segs) >= 6
    residual = g.cubic_residual([loop], segs, samples=16)
    assert residual <= 1.5


def test_simplify_closed_keeps_corners():
    alpha = g.decode_raster(fx.triangle_png())
    loop = g.extract_contours(alpha, 0.5)[0]
    simple = g.simplify_closed(loop, tol=1.0)
    # a triangle simplifies to ~3 points, never below 4 by contract
    assert 4 <= len(simple) <= 8


# ---------------------------------------------------------------------------
# rasterization roundtrip
# ---------------------------------------------------------------------------

def test_rasterize_glyph_iou_circle(circle_alpha, circle_fit):
    size = 1024
    baseline = g.estimate_baseline(size)
    contour_units = _to_units_contour(circle_fit, size, baseline)
    rast = g.rasterize_glyph([contour_units], 600.0, size, baseline)
    assert rast.shape == (size, size)
    mask = circle_alpha >= 0.5
    rm = rast >= 0.5
    inter = np.count_nonzero(rm & mask)
    union = np.count_nonzero(rm | mask)
    assert inter / union >= 0.90


def test_rasterize_empty_glyph():
    rast = g.rasterize_glyph([], 250.0, 256, g.estimate_baseline(256))
    assert rast.shape == (256, 256)
    assert not rast.any()


# ---------------------------------------------------------------------------
# confidence checks
# ---------------------------------------------------------------------------

def _circle_candidate(circle_fit):
    size = 1024
    baseline = g.estimate_baseline(size)
    contour_units = _to_units_contour(circle_fit, size, baseline)
    return g.CandidateGlyph(
        gid="uni004F",
        unicode=0x4F,
        contours_units=[contour_units],
        advance=700.0,
        lsb=50.0,
        bbox=(0.0, 0.0, 0.0, 0.0),
        fit_error=1.0,
    )


def test_confidence_pass_circle(circle_loop, circle_fit):
    size = 1024
    baseline = g.estimate_baseline(size)
    cand = _circle_candidate(circle_fit)
    src_polys = [np.array([g.px_to_units(p[0], p[1], size, baseline) for p in circle_loop])]
    meta = {
        "components": 1,
        "holes": 0,
        "source_polylines": src_polys,
        "fit_tol": 1.0 * 1000.0 / size,
    }
    report = g.confidence_checks(cand, meta)
    assert report.passed, report.failures
    assert report.failures == []
    assert report.metrics["components"] == 1
    assert report.metrics["holes"] == 0
    assert report.metrics["self_intersections"] == 0
    assert report.metrics["edge_agreement"] >= 0.80
    assert report.metrics["bezier_residual"] is not None


def test_confidence_fail_open_contour(circle_fit):
    cand = _circle_candidate(circle_fit)
    segs = list(cand.contours_units[0])
    last = segs[-1]
    segs[-1] = (last[0], last[1], last[2], (last[3][0] + 5.0, last[3][1]))
    cand.contours_units = [segs]
    report = g.confidence_checks(cand, {})
    assert not report.passed
    assert "not_closed" in report.failures


def test_confidence_fail_nan_coord(circle_fit):
    cand = _circle_candidate(circle_fit)
    segs = cand.contours_units[0]
    bad = list(segs)
    p0, p1, p2, p3 = bad[0]
    bad[0] = (p0, (float("nan"), p1[1]), p2, p3)
    cand.contours_units = [bad]
    report = g.confidence_checks(cand, {})
    assert not report.passed
    assert "non_finite" in report.failures


def test_confidence_fail_advance_zero(circle_fit):
    cand = _circle_candidate(circle_fit)
    cand.advance = 0.0
    report = g.confidence_checks(cand, {})
    assert not report.passed
    assert "advance_range" in report.failures


def test_confidence_fail_topology(circle_loop, circle_fit):
    cand = _circle_candidate(circle_fit)
    report = g.confidence_checks(cand, {"components": 3, "holes": 2})
    assert not report.passed
    assert "component_count" in report.failures
    assert "hole_count" in report.failures


def test_confidence_space_advance_rule():
    cand = g.CandidateGlyph(
        gid="space", unicode=32, contours_units=[], advance=250.0,
        lsb=0.0, bbox=(0, 0, 0, 0), fit_error=0.0,
    )
    report = g.confidence_checks(cand, {"components": 0, "holes": 0})
    assert report.passed, report.failures
