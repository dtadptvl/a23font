"""Font binary builders (fast15.md): canonical FontModel -> OTF (CFF cubic)
and TTF (glyf quadratic via cu2qu).

Ported and reworked from the author's prior engine (E:/cv/myfonts/engine/
builder.py, read-only source). Known weaknesses fixed:
  * the old builder passed glyph.lsb (a hardcoded/stale field) into the hmtx
    metrics tuple; here hmtx = (advance, xMin) with xMin computed from the
    ACTUAL sampled contour bbox (lsb falls back to 0 for empty glyphs such
    as space), matching what glyf/CFF really contain.
  * the old cubic->quadratic fallback used tuple arithmetic that would crash
    ((p1 + p2) on tuples concatenates); removed in favor of letting cu2qu
    exceptions surface.
Structure follows fontTools.fontBuilder.FontBuilder canonical order exactly.
"""
from __future__ import annotations

import os
import re
import unicodedata
from typing import Dict, List, Tuple

from fontTools.cu2qu import curve_to_quadratic
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen

from pipeline.fontmodel import FontModel, GlyphModel

__all__ = ["build_otf", "build_ttf", "sanitize_ps_name"]

_NOTDEF_ADVANCE = 500


def sanitize_ps_name(text: str) -> str:
    """PostScript name: ASCII only, no spaces, safe character set."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9-]", "", text.replace(" ", ""))
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "Custom"


def _default_notdef_pen(pen) -> None:
    """Box .notdef: outer rectangle + reversed inner rectangle (outline ring)."""
    pen.moveTo((50, 0))
    pen.lineTo((50, 800))
    pen.lineTo((450, 800))
    pen.lineTo((450, 0))
    pen.closePath()
    pen.moveTo((100, 50))
    pen.lineTo((400, 50))
    pen.lineTo((400, 750))
    pen.lineTo((100, 750))
    pen.closePath()


def _inked_lsb(glyph: GlyphModel) -> int:
    """hmtx lsb from the actual contour bbox; 0 fallback for empty glyphs."""
    if not glyph.contours:
        return 0
    xs: List[float] = []
    for contour in glyph.contours:
        for p0, p1, p2, p3 in contour:
            xs.extend((p0[0], p1[0], p2[0], p3[0]))
            for k in range(1, 8):
                t = k / 8.0
                u = 1.0 - t
                xs.append(
                    u**3 * p0[0] + 3 * u * u * t * p1[0]
                    + 3 * u * t * t * p2[0] + t**3 * p3[0]
                )
    return int(round(min(xs)))


def _prepare(model: FontModel) -> Tuple[int, List[str], Dict[str, Tuple[int, int]]]:
    """Glyph order (.notdef first) + hmtx metrics computed from real bounds."""
    upem = int(model.upem or model.global_metrics.get("unitsPerEm", 1000))
    order = [".notdef"] + [g for g in model.glyph_order if g != ".notdef"]
    # include any glyphs missing from glyph_order deterministically
    for name in sorted(model.glyphs):
        if name not in order:
            order.append(name)
    metrics: Dict[str, Tuple[int, int]] = {}
    for name in order:
        glyph = model.glyphs.get(name)
        if name == ".notdef" and glyph is None:
            metrics[name] = (_NOTDEF_ADVANCE, 50)
            continue
        if glyph is None or not glyph.contours:
            advance = int(glyph.advance) if glyph is not None else _NOTDEF_ADVANCE
            metrics[name] = (max(0, advance), 0)
        else:
            metrics[name] = (max(0, int(glyph.advance)), _inked_lsb(glyph))
    return upem, order, metrics


def _name_table(model: FontModel) -> Dict[str, str]:
    family = model.metadata.get("familyName", "A23Font")
    style = model.metadata.get("styleName", "Regular")
    ps_name = sanitize_ps_name(model.metadata.get("psName", f"{family}-{style}"))
    full_name = model.metadata.get("fullName", f"{family} {style}")
    return {
        "familyName": family,
        "styleName": style,
        "uniqueFontIdentifier": f"1.000;A23;{ps_name}",
        "fullName": full_name,
        "psName": ps_name,
        "version": "Version 1.000",
    }


def _setup_common(fb: FontBuilder, model: FontModel, metrics: Dict[str, Tuple[int, int]]) -> None:
    fb.setupHorizontalMetrics(metrics)
    asc = int(model.global_metrics.get("ascender", 800))
    desc = int(model.global_metrics.get("descender", -200))
    gap = int(model.global_metrics.get("line_gap", 0))
    fb.setupHorizontalHeader(ascent=asc, descent=desc)
    fb.setupNameTable(_name_table(model))
    fb.setupOS2(
        sTypoAscender=asc,
        sTypoDescender=desc,
        sTypoLineGap=gap,
        usWinAscent=max(asc, 0),
        usWinDescent=abs(desc),
    )
    fb.setupPost()


def _save(fb: FontBuilder, out_path: str) -> str:
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    fb.save(out_path)
    return out_path


def build_otf(model: FontModel, out_path: str) -> str:
    """Build OpenType/CFF (cubic preserved) from the canonical model."""
    upem, order, metrics = _prepare(model)
    fb = FontBuilder(unitsPerEm=upem, isTTF=False)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(model.cmap)

    charstrings = {}
    notdef_pen = T2CharStringPen(_NOTDEF_ADVANCE, None)
    _default_notdef_pen(notdef_pen)
    charstrings[".notdef"] = notdef_pen.getCharString()

    for name in order:
        if name == ".notdef":
            continue
        glyph = model.glyphs.get(name)
        advance = metrics[name][0]
        pen = T2CharStringPen(advance, None)
        if glyph is not None:
            for contour in glyph.contours:
                if not contour:
                    continue
                pen.moveTo((float(contour[0][0][0]), float(contour[0][0][1])))
                for _p0, p1, p2, p3 in contour:
                    pen.curveTo(
                        (float(p1[0]), float(p1[1])),
                        (float(p2[0]), float(p2[1])),
                        (float(p3[0]), float(p3[1])),
                    )
                pen.closePath()
        charstrings[name] = pen.getCharString()

    ps_name = sanitize_ps_name(model.metadata.get("psName", _name_table(model)["psName"]))
    fb.setupCFF(
        psName=ps_name,
        fontInfo={
            "FullName": _name_table(model)["fullName"],
            "FamilyName": model.metadata.get("familyName", "A23Font"),
        },
        privateDict={},
        charStringsDict=charstrings,
    )
    _setup_common(fb, model, metrics)
    return _save(fb, out_path)


def _cubic_to_quads(seg, max_err: float = 1.0):
    """One cubic -> quadratic point chains via cu2qu (max_err in UPEM units)."""
    pts = [(float(p[0]), float(p[1])) for p in seg]
    quad_points = curve_to_quadratic(pts, max_err)
    quads = []
    for i in range(1, len(quad_points) - 1, 2):
        quads.append((quad_points[i], quad_points[i + 1]))
    return quads


def build_ttf(model: FontModel, out_path: str) -> str:
    """Build TrueType/glyf (cubic -> quadratic via cu2qu, max_err 1.0 units)."""
    upem, order, metrics = _prepare(model)
    fb = FontBuilder(unitsPerEm=upem, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(model.cmap)

    glyphs = {}
    notdef_pen = TTGlyphPen(None)
    _default_notdef_pen(notdef_pen)
    glyphs[".notdef"] = notdef_pen.glyph()

    for name in order:
        if name == ".notdef":
            continue
        glyph = model.glyphs.get(name)
        pen = TTGlyphPen(None)
        if glyph is not None:
            for contour in glyph.contours:
                if not contour:
                    continue
                start = contour[0][0]
                pen.moveTo((float(start[0]), float(start[1])))
                for seg in contour:
                    for ctrl, end in _cubic_to_quads(seg, 1.0):
                        pen.qCurveTo(
                            (float(ctrl[0]), float(ctrl[1])),
                            (float(end[0]), float(end[1])),
                        )
                pen.closePath()
        glyphs[name] = pen.glyph()

    fb.setupGlyf(glyphs)
    _setup_common(fb, model, metrics)
    return _save(fb, out_path)
