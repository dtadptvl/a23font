"""M3.A2 build + validation tests: FontModel -> TTF/OTF -> engine validation.

Fully offline: builds a 4-glyph font (.notdef, space, uni004F ring fitted
from the ring raster fixture, uni0048 H fitted from the H raster fixture),
then validates the binaries with fontTools (both), HarfBuzz and FreeType
(the ONE heavy route on the TTF, per fast15.md). Optional engines degrade
gracefully to skipped when absent.
"""
from __future__ import annotations

import numpy as np
import pytest
from fontTools.ttLib import TTFont

from pipeline import geometry as g
from pipeline.build import build_otf, build_ttf, sanitize_ps_name
from pipeline.fontmodel import FontModel, GlyphModel
from pipeline.validate import (
    CORPUS_BASE,
    fonttools_validate,
    freetype_validate,
    harfbuzz_validate,
    heavy_validation,
)
from tests import geometry_fixtures as fx

SIZE = 1024


def _fit_glyph(name: str, unicode_cp: int, advance: int, png: bytes) -> GlyphModel:
    """Raster -> decode -> extract -> fit -> units -> GlyphModel."""
    baseline = g.estimate_baseline(SIZE)
    alpha = g.decode_raster(png)
    loops = g.extract_contours(alpha, 0.5)
    assert loops, f"no contours extracted for {name}"
    contours_units = []
    for loop in loops:
        segs = g.fit_closed_cubic(loop, error_tol=1.0)
        assert segs, f"fit produced no segments for {name}"
        contours_units.append(
            [tuple(g.px_to_units(p[0], p[1], SIZE, baseline) for p in seg) for seg in segs]
        )
    return GlyphModel(
        name=name,
        unicode_cp=unicode_cp,
        advance=advance,
        contours=contours_units,
        status="RECONSTRUCTED",
        confidence=1.0,
    )


@pytest.fixture(scope="module")
def four_glyph_model():
    model = FontModel(
        metadata={
            "familyName": "A23 Test",
            "styleName": "Regular",
            "psName": "A23 Test Regular",  # needs sanitization (space)
            "fullName": "A23 Test Regular",
        }
    )
    model.add_glyph(GlyphModel(name=".notdef"))
    model.add_glyph(GlyphModel(name="space", unicode_cp=32, advance=250))
    model.add_glyph(_fit_glyph("uni004F", 0x4F, 700, fx.ring_png()))
    model.add_glyph(_fit_glyph("uni0048", 0x48, 750, fx.letter_h_png()))
    return model


@pytest.fixture(scope="module")
def built(four_glyph_model, tmp_path_factory):
    out = tmp_path_factory.mktemp("fonts")
    ttf = build_ttf(four_glyph_model, str(out / "a23test.ttf"))
    otf = build_otf(four_glyph_model, str(out / "a23test.otf"))
    return ttf, otf


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def test_sanitize_ps_name():
    assert sanitize_ps_name("A23 Test Regular") == "A23TestRegular"
    assert sanitize_ps_name("  --x--  ") == "x"
    assert sanitize_ps_name("Ünïcødé Fönt") == "UnicdeFont"  # NFKD keeps ASCII base letters
    assert sanitize_ps_name("") == "Custom"


def test_build_outputs_load(four_glyph_model, built):
    ttf, otf = built
    font_ttf = TTFont(ttf)
    font_otf = TTFont(otf)
    assert font_ttf.getGlyphOrder() == [".notdef", "space", "uni004F", "uni0048"]
    assert font_otf.getGlyphOrder() == [".notdef", "space", "uni004F", "uni0048"]


def test_cmap_codepoints(built):
    ttf, otf = built
    for path in (ttf, otf):
        cmap = TTFont(path).getBestCmap()
        assert 0x4F in cmap and cmap[0x4F] == "uni004F"
        assert 0x48 in cmap and cmap[0x48] == "uni0048"


def test_ttf_quadratic_and_otf_cubic_table_keys(built):
    ttf, otf = built
    ttf_tables = set(TTFont(ttf).keys())
    otf_tables = set(TTFont(otf).keys())
    # TTF: glyf quadratic outlines, no CFF
    assert "glyf" in ttf_tables and "CFF " not in ttf_tables
    # OTF: CFF cubic outlines, no glyf
    assert "CFF " in otf_tables and "glyf" not in otf_tables


def test_hmtx_lsb_from_real_bbox(four_glyph_model, built):
    ttf, _ = built
    font = TTFont(ttf)
    metrics = font["hmtx"].metrics
    ring_glyph = four_glyph_model.glyphs["uni004F"]
    # hmtx lsb equals the actual contour xMin (not a hardcoded field)
    assert metrics["uni004F"][0] == 700
    assert metrics["uni004F"][1] == ring_glyph.xMin
    assert metrics["space"] == (250, 0)  # empty glyph: lsb fallback 0


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def test_fonttools_validate_ttf(built):
    ttf, _ = built
    result = fonttools_validate(ttf)
    assert result["passed"] is True, result["errors"]
    assert result["errors"] == []
    assert result["glyph_count"] == 4
    assert result["cmap_entries"] >= 3  # space + O + H
    for table in ("head", "hhea", "maxp", "OS/2", "name", "cmap", "post"):
        assert table in result["tables"]


def test_fonttools_validate_otf(built):
    _, otf = built
    result = fonttools_validate(otf)
    assert result["passed"] is True, result["errors"]
    assert result["errors"] == []
    assert result["glyph_count"] == 4


def test_harfbuzz_validate_corpus(built):
    ttf, _ = built
    result = harfbuzz_validate(ttf, CORPUS_BASE)
    assert result["passed"] is True, result.get("details")
    if not result.get("skipped"):
        assert len(result["shaped"]) == len(CORPUS_BASE)


def test_freetype_validate_render(built):
    ttf, _ = built
    result = freetype_validate(ttf, "".join(CORPUS_BASE))
    assert result["passed"] is True, result.get("details")
    if not result.get("skipped"):
        assert result["ink_glyphs"] > 0
        assert result["empty_glyphs"] <= 0.2 * result["ink_glyphs"]


def test_heavy_validation_overall(four_glyph_model, built):
    ttf, otf = built
    report = heavy_validation(four_glyph_model, ttf, otf, vietnamese=False)
    assert report.passed is True, report.to_dict()
    data = report.to_dict()
    assert data["fonttools_ttf"]["passed"] is True
    assert data["fonttools_otf"]["passed"] is True
    assert isinstance(data["skipped_engines"], list)


def test_heavy_validation_reports_errors_verbatim(tmp_path):
    bogus = tmp_path / "bogus.ttf"
    bogus.write_bytes(b"not a font at all")
    result = fonttools_validate(str(bogus))
    assert result["passed"] is False
    assert result["errors"], "load failure must be reported verbatim"
    assert "fonttools load exception" in result["errors"][0]


def test_name_and_metrics_tables(built):
    ttf, _ = built
    font = TTFont(ttf)
    name = font["name"]
    family = name.getDebugName(1)
    assert family == "A23 Test"
    ps = name.getDebugName(6)
    assert ps == "A23TestRegular"  # sanitized ASCII, no spaces
    os2 = font["OS/2"]
    assert os2.sTypoAscender == 800
    assert os2.sTypoDescender == -200
    assert font["head"].unitsPerEm == 1000
