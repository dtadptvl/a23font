"""Canonical FontModel tests: glyph management, cmap, bounds, JSON roundtrip."""
from __future__ import annotations

import json

import pytest

from pipeline.fontmodel import FontModel, GlyphModel, suggest_glyph_name


def _square_contour(x0=100.0, y0=0.0, x1=600.0, y1=400.0):
    """One closed contour of straight cubic segments (controls on the line)."""
    pts = [(x0, y0), (x0, y1), (x1, y1), (x1, y0)]
    segs = []
    for i in range(4):
        a = pts[i]
        b = pts[(i + 1) % 4]
        c1 = (a[0] + (b[0] - a[0]) / 3.0, a[1] + (b[1] - a[1]) / 3.0)
        c2 = (a[0] + 2.0 * (b[0] - a[0]) / 3.0, a[1] + 2.0 * (b[1] - a[1]) / 3.0)
        segs.append((a, c1, c2, b))
    return segs


@pytest.fixture()
def model():
    m = FontModel(metadata={"familyName": "A23 Test", "styleName": "Regular"})
    m.add_glyph(GlyphModel(name=".notdef"))
    m.add_glyph(GlyphModel(name="space", unicode_cp=32, advance=250))
    m.add_glyph(
        GlyphModel(
            name="uni004F",
            unicode_cp=0x4F,
            advance=700,
            contours=[_square_contour()],
        )
    )
    return m


def test_add_glyph_order_and_cmap(model):
    assert model.glyph_order[0] == ".notdef"
    assert model.glyph_order == [".notdef", "space", "uni004F"]
    assert model.cmap == {32: "space", 0x4F: "uni004F"}
    assert model.get_glyph_by_unicode(0x4F).name == "uni004F"
    assert model.get_glyph_by_char(" ").name == "space"
    assert model.get_glyph_by_unicode(0x55) is None


def test_compute_bounds_sampled(model):
    glyph = model.glyphs["uni004F"]
    assert glyph.xMin == 100
    assert glyph.yMin == 0
    assert glyph.xMax == 600
    assert glyph.yMax == 400
    assert glyph.lsb == 100  # lsb derived from real bbox
    empty = model.glyphs["space"]
    assert (empty.xMin, empty.yMin, empty.xMax, empty.yMax) == (0, 0, 0, 0)


def test_default_global_metrics(model):
    assert model.global_metrics["ascender"] == 800
    assert model.global_metrics["descender"] == -200
    assert model.global_metrics["line_gap"] == 0
    assert model.upem == 1000


def test_to_dict_from_dict_roundtrip(model):
    model.kerning[("uni004F", "space")] = -25
    model.glyphs["uni004F"].anchors["top"] = (350.12345, 400.98765)
    data = model.to_dict()
    # deterministic key order + json-serializable
    text = json.dumps(data, sort_keys=True)
    restored = FontModel.from_dict(json.loads(text))
    assert restored.to_dict() == data
    # floats rounded to 3
    anchor = restored.glyphs["uni004F"].anchors["top"]
    assert anchor == (350.123, 400.988)


def test_roundtrip_preserves_semantics(model):
    restored = FontModel.from_dict(model.to_dict())
    assert restored.get_glyph_by_unicode(0x4F).advance == 700
    assert restored.glyphs["uni004F"].contours[0][0][0] == (100.0, 0.0)
    assert restored.global_metrics["ascender"] == 800


def test_glyph_naming_rules():
    assert suggest_glyph_name(None, 5) == "gid_5"
    assert suggest_glyph_name(32) == "space"
    assert suggest_glyph_name(0x4F) == "uni004F"
    assert suggest_glyph_name(0x10400) == "u010400"


def test_glyph_naming_uniqueness(model):
    dup = GlyphModel(name="space", unicode_cp=0x2423, advance=120)
    model.add_glyph(dup)
    assert dup.name == "space_2"
    assert model.cmap[0x2423] == "space_2"
    dup2 = GlyphModel(name="space", unicode_cp=0x2000, advance=120)
    model.add_glyph(dup2)
    assert dup2.name == "space_3"


def test_notdef_placeholder_not_renamed(model):
    assert ".notdef" in model.glyphs
    assert ".notdef_2" not in model.glyphs


def test_status_and_confidence_fields(model):
    glyph = model.glyphs["uni004F"]
    assert glyph.status in {"OBSERVED", "RECONSTRUCTED", "INFERRED", "SYNTHESIZED"}
    assert 0.0 <= glyph.confidence <= 1.0
