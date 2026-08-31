"""Canonical FontModel (fast15.md): cubic master representation.

Ported and reworked from the author's prior engine (E:/cv/myfonts/engine/
font_model.py, read-only source). Changes vs the old model:
  * compute_bounds() uses a SAMPLED bbox (curve points at t=k/8 plus control
    points) instead of control-point-only bounds, which overstate the true
    glyph extent for curved outlines.
  * JSON roundtrip (to_dict/from_dict) with floats rounded to 3 decimals and
    deterministic key ordering so cached models hash stably.
  * glyph naming with uniqueness guarantees (.notdef / space / uniXXXX /
    gid_<n>).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "GlyphModel",
    "FontModel",
    "suggest_glyph_name",
]

VALID_STATUS = {"OBSERVED", "RECONSTRUCTED", "INFERRED", "SYNTHESIZED"}

Point = Tuple[float, float]
CubicSeg = Tuple[Point, Point, Point, Point]


def _r3(v: float) -> float:
    return round(float(v), 3)


def suggest_glyph_name(unicode_cp: Optional[int], ordinal: int = 0) -> str:
    """Canonical glyph name: .notdef / space / uniXXXX / gid_<n>."""
    if unicode_cp is None:
        return f"gid_{ordinal}"
    if unicode_cp == 32:
        return "space"
    if unicode_cp <= 0xFFFF:
        return f"uni{unicode_cp:04X}"
    return f"u{unicode_cp:06X}"


@dataclass
class GlyphModel:
    """One glyph in canonical cubic-Bezier master format (UPEM=1000, y up).

    contours: list of contours; each contour is a list of cubic segments
    ((x,y) x 4, floats).
    """

    name: str
    unicode_cp: Optional[int] = None
    advance: int = 500
    lsb: int = 0
    xMin: int = 0
    yMin: int = 0
    xMax: int = 0
    yMax: int = 0
    contours: List[List[CubicSeg]] = field(default_factory=list)
    anchors: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    status: str = "RECONSTRUCTED"
    confidence: float = 1.0

    def compute_bounds(self) -> Tuple[int, int, int, int]:
        """Sampled bbox from control AND on-curve points (t = k/8 samples).

        Sets xMin/yMin/xMax/yMax and derives lsb from xMin for inked glyphs.
        Empty glyphs keep zero bounds and their existing lsb (0 by default).
        """
        if not self.contours:
            self.xMin = self.yMin = self.xMax = self.yMax = 0
            return (0, 0, 0, 0)
        xs: List[float] = []
        ys: List[float] = []
        for contour in self.contours:
            for p0, p1, p2, p3 in contour:
                for p in (p0, p1, p2, p3):
                    xs.append(float(p[0]))
                    ys.append(float(p[1]))
                # sampled on-curve points tighten/verify the hull
                for k in range(1, 8):
                    t = k / 8.0
                    u = 1.0 - t
                    x = u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0]
                    y = u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]
                    xs.append(x)
                    ys.append(y)
        self.xMin = int(round(min(xs)))
        self.yMin = int(round(min(ys)))
        self.xMax = int(round(max(xs)))
        self.yMax = int(round(max(ys)))
        self.lsb = self.xMin
        return (self.xMin, self.yMin, self.xMax, self.yMax)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "advance": int(self.advance),
            "anchors": {k: [_r3(v[0]), _r3(v[1])] for k, v in sorted(self.anchors.items())},
            "confidence": _r3(self.confidence),
            "contours": [
                [[[_r3(x), _r3(y)] for (x, y) in seg] for seg in contour]
                for contour in self.contours
            ],
            "lsb": int(self.lsb),
            "name": self.name,
            "status": self.status,
            "unicode_cp": self.unicode_cp,
            "xMax": int(self.xMax),
            "xMin": int(self.xMin),
            "yMax": int(self.yMax),
            "yMin": int(self.yMin),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GlyphModel":
        contours = [
            [tuple(tuple(float(c) for c in pt) for pt in seg) for seg in contour]
            for contour in data.get("contours", [])
        ]
        anchors = {k: (float(v[0]), float(v[1])) for k, v in (data.get("anchors") or {}).items()}
        return cls(
            name=data["name"],
            unicode_cp=data.get("unicode_cp"),
            advance=int(data.get("advance", 500)),
            lsb=int(data.get("lsb", 0)),
            xMin=int(data.get("xMin", 0)),
            yMin=int(data.get("yMin", 0)),
            xMax=int(data.get("xMax", 0)),
            yMax=int(data.get("yMax", 0)),
            contours=contours,
            anchors=anchors,
            status=data.get("status", "RECONSTRUCTED"),
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass
class FontModel:
    """Canonical cubic font model: single source of truth before TTF/OTF export."""

    upem: int = 1000
    metadata: Dict[str, str] = field(default_factory=dict)
    global_metrics: Dict[str, int] = field(default_factory=dict)
    glyphs: Dict[str, GlyphModel] = field(default_factory=dict)
    glyph_order: List[str] = field(default_factory=lambda: [".notdef"])
    cmap: Dict[int, str] = field(default_factory=dict)
    kerning: Dict[Tuple[str, str], int] = field(default_factory=dict)
    features: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.global_metrics.setdefault("ascender", 800)
        self.global_metrics.setdefault("descender", -200)
        self.global_metrics.setdefault("line_gap", 0)
        self.global_metrics.setdefault("unitsPerEm", self.upem)

    # -- glyph management ---------------------------------------------------

    def unique_name(self, base: str) -> str:
        """Return `base` or a suffixed variant not already used."""
        if base not in self.glyphs and base not in self.glyph_order:
            return base
        i = 2
        while f"{base}_{i}" in self.glyphs or f"{base}_{i}" in self.glyph_order:
            i += 1
        return f"{base}_{i}"

    def add_glyph(self, glyph: GlyphModel) -> GlyphModel:
        """Insert glyph, compute bounds, and register its cmap entry."""
        fills_placeholder = glyph.name == ".notdef" and ".notdef" not in self.glyphs
        if not fills_placeholder:
            glyph.name = self.unique_name(glyph.name)
        glyph.compute_bounds()
        self.glyphs[glyph.name] = glyph
        if glyph.name not in self.glyph_order:
            self.glyph_order.append(glyph.name)
        if glyph.unicode_cp is not None:
            self.cmap[int(glyph.unicode_cp)] = glyph.name
        return glyph

    def get_glyph_by_unicode(self, cp: int) -> Optional[GlyphModel]:
        name = self.cmap.get(int(cp))
        return self.glyphs.get(name) if name else None

    def get_glyph_by_char(self, ch: str) -> Optional[GlyphModel]:
        return self.get_glyph_by_unicode(ord(ch[0])) if ch else None

    # -- JSON roundtrip -------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cmap": {str(cp): self.cmap[cp] for cp in sorted(self.cmap)},
            "features": self.features,
            "global_metrics": dict(sorted(self.global_metrics.items())),
            "glyph_order": list(self.glyph_order),
            "glyphs": {name: self.glyphs[name].to_dict() for name in sorted(self.glyphs)},
            "kerning": [
                [left, right, int(value)]
                for (left, right), value in sorted(self.kerning.items())
            ],
            "metadata": dict(sorted(self.metadata.items())),
            "upem": int(self.upem),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FontModel":
        model = cls(
            upem=int(data.get("upem", 1000)),
            metadata=dict(data.get("metadata") or {}),
            global_metrics={k: int(v) for k, v in (data.get("global_metrics") or {}).items()},
            glyph_order=list(data.get("glyph_order") or [".notdef"]),
            cmap={int(cp): name for cp, name in (data.get("cmap") or {}).items()},
            kerning={
                (item[0], item[1]): int(item[2]) for item in (data.get("kerning") or [])
            },
            features=dict(data.get("features") or {}),
        )
        model.glyphs = {
            name: GlyphModel.from_dict(gdata)
            for name, gdata in (data.get("glyphs") or {}).items()
        }
        return model
