"""Shared raster fixtures for the M3 geometry tests (fast15 offline path).

All fixtures are synthetic glyphs rendered by PIL at 4x resolution and
downsampled with bilinear filtering, producing antialiased alpha edges the
way real font rasters look (black foreground on white background, per the
source convention decoded by pipeline.geometry.decode_raster).
"""
from __future__ import annotations

import io
from typing import Tuple

from PIL import Image, ImageDraw

CANVAS = 1024
CENTER = CANVAS // 2
SS = 4  # supersampling factor


def _finalize(big: Image.Image) -> bytes:
    img = big.resize((CANVAS, CANVAS), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _canvas() -> Tuple[Image.Image, ImageDraw.ImageDraw]:
    big = Image.new("L", (SS * CANVAS, SS * CANVAS), 255)
    return big, ImageDraw.Draw(big)


def circle_png(radius: int = 300) -> bytes:
    """Solid circle centered on the canvas."""
    big, d = _canvas()
    r = SS * radius
    d.ellipse([SS * CENTER - r, SS * CENTER - r, SS * CENTER + r, SS * CENTER + r], fill=0)
    return _finalize(big)


def ring_png(outer: int = 300, inner: int = 150) -> bytes:
    """Ring: outer disk with a concentric hole."""
    big, d = _canvas()
    ro, ri = SS * outer, SS * inner
    d.ellipse([SS * CENTER - ro, SS * CENTER - ro, SS * CENTER + ro, SS * CENTER + ro], fill=0)
    d.ellipse([SS * CENTER - ri, SS * CENTER - ri, SS * CENTER + ri, SS * CENTER + ri], fill=255)
    return _finalize(big)


def rounded_square_png(half: int = 280, radius: int = 90) -> bytes:
    """Rounded square centered on the canvas."""
    big, d = _canvas()
    h, r = SS * half, SS * radius
    d.rounded_rectangle(
        [SS * CENTER - h, SS * CENTER - h, SS * CENTER + h, SS * CENTER + h],
        radius=r,
        fill=0,
    )
    return _finalize(big)


def triangle_png() -> bytes:
    """Isosceles triangle (3 sharp corners)."""
    big, d = _canvas()
    pts = [(SS * 512, SS * 119), (SS * 812, SS * 819), (SS * 212, SS * 819)]
    d.polygon(pts, fill=0)
    return _finalize(big)


def letter_l_png() -> bytes:
    """Letter L with 6 sharp corners."""
    big, d = _canvas()
    pts = [
        (SS * 312, SS * 119),
        (SS * 442, SS * 119),
        (SS * 442, SS * 689),
        (SS * 762, SS * 689),
        (SS * 762, SS * 819),
        (SS * 312, SS * 819),
    ]
    d.polygon(pts, fill=0)
    return _finalize(big)


def letter_h_png() -> bytes:
    """Letter H (two stems + crossbar union), 12 sharp corners."""
    big, d = _canvas()
    d.rectangle([SS * 182, SS * 119, SS * 312, SS * 819], fill=0)
    d.rectangle([SS * 712, SS * 119, SS * 842, SS * 819], fill=0)
    d.rectangle([SS * 312, SS * 419, SS * 712, SS * 539], fill=0)
    return _finalize(big)
