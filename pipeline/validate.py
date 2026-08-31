"""Binary font validation (fast15.md heavy validation route).

Three engines:
  * fontTools  - structural/load validation, always available (required dep)
  * HarfBuzz   - shaping validation, optional import; skipped gracefully
  * FreeType   - rasterization validation, optional import; skipped gracefully

Per fast15.md the heavy route runs ONCE on the temporary TTF (HarfBuzz +
FreeType) plus cheap FontTools structure checks on BOTH final binaries.
Missing optional engines never fail the build on the dev host; they are
recorded as skipped (on the A23 host the engines are present).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from fontTools.pens.basePen import NullPen
from fontTools.ttLib import TTFont

__all__ = [
    "CORPUS_BASE",
    "CORPUS_VIETNAMESE",
    "fonttools_validate",
    "harfbuzz_validate",
    "freetype_validate",
    "heavy_validation",
    "ValidationReport",
]

# fast15.md base corpus (representative glyphs, digits, punctuation,
# AVATAR / Hamburgefontsiv / spacing-sensitive pairs / ligature probes)
CORPUS_BASE: List[str] = [
    "Hamburgefontsiv",
    "AVATAR",
    "VA WA To Ta Te Ty",
    "fi fl ff ffi ffl",
    "0123456789",
    ".,;:!?()",
]

CORPUS_VIETNAMESE: List[str] = [
    "Việt Nam đất nước",
    "Ắ Ằ Ẳ Ẵ Ặ",
    "Ấ Ầ Ẩ Ẫ Ậ",
    "Ế Ề Ể Ễ Ệ",
    "Ố Ồ Ổ Ỗ Ộ",
    "Ớ Ờ Ở Ỡ Ợ",
    "Ứ Ừ Ử Ữ Ự",
]

REQUIRED_TABLES = ["head", "hhea", "maxp", "OS/2", "name", "cmap", "post"]


def fonttools_validate(path: str) -> Dict[str, Any]:
    """Structural validation: required tables, cmap, outline load, metrics."""
    result: Dict[str, Any] = {
        "passed": False,
        "errors": [],
        "warnings": [],
        "tables": [],
        "glyph_count": 0,
        "cmap_entries": 0,
    }
    try:
        font = TTFont(path)
    except Exception as exc:  # noqa: BLE001 - report verbatim
        result["errors"].append(f"fonttools load exception: {exc}")
        return result

    result["tables"] = sorted(font.keys())
    for table in REQUIRED_TABLES:
        if table not in font:
            result["errors"].append(f"missing required table: {table}")

    cmap = font.getBestCmap()
    if not cmap:
        result["errors"].append("best cmap empty or missing")
    else:
        result["cmap_entries"] = len(cmap)

    order = font.getGlyphOrder()
    result["glyph_count"] = len(order)
    if len(order) < 2:
        result["errors"].append(f"invalid glyph count: {len(order)}")

    # force outline load for ALL glyphs
    try:
        glyph_set = font.getGlyphSet()
        pen = NullPen()
        for name in order:
            glyph_set[name].draw(pen)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"outline load exception: {exc}")

    # metrics sanity
    try:
        hmtx = font["hmtx"].metrics
        for name in order:
            advance = hmtx[name][0]
            if advance < 0:
                result["errors"].append(f"negative advance for {name}: {advance}")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"hmtx sanity exception: {exc}")

    result["passed"] = not result["errors"]
    return result


def _cmap_codepoints(path: str) -> set:
    try:
        return set(TTFont(path).getBestCmap().keys())
    except Exception:  # noqa: BLE001
        return set()


def harfbuzz_validate(path: str, corpus: List[str]) -> Dict[str, Any]:
    """Shape the corpus; codepoints present in cmap must not map to GID 0."""
    try:
        import uharfbuzz as hb
    except ImportError:
        return {
            "passed": True,
            "skipped": True,
            "details": "uharfbuzz not installed on this interpreter; skipped",
            "shaped": [],
        }

    result: Dict[str, Any] = {"passed": False, "skipped": False, "details": "", "shaped": []}
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        face = hb.Face(data)
        font = hb.Font(face)
        font.scale = (1000, 1000)
        cmap_cps = _cmap_codepoints(path)

        shaped = []
        for text in corpus:
            buf = hb.Buffer()
            buf.add_str(text)
            buf.guess_segment_properties()
            hb.shape(font, buf)
            infos = buf.glyph_infos
            shaped.append({"text": text[:32], "glyph_count": len(infos)})
        result["shaped"] = shaped

        # every codepoint PRESENT IN CMAP must shape to a real glyph (gid != 0)
        bad = []
        seen = set()
        full = "".join(corpus)
        for ch in full:
            cp = ord(ch)
            if cp in cmap_cps and cp not in seen:
                seen.add(cp)
                buf = hb.Buffer()
                buf.add_str(ch)
                buf.guess_segment_properties()
                hb.shape(font, buf)
                if any(info.codepoint == 0 for info in buf.glyph_infos):
                    bad.append(f"U+{cp:04X}")
        if bad:
            result["details"] = "mapped to GID 0 despite cmap presence: " + ",".join(bad)
            result["passed"] = False
        else:
            result["details"] = f"all {len(seen)} cmap-present corpus codepoints shape to real glyphs"
            result["passed"] = True
    except Exception as exc:  # noqa: BLE001
        result["details"] = f"harfbuzz exception: {exc}"
        result["passed"] = False
    return result


def freetype_validate(path: str, sample_text: str) -> Dict[str, Any]:
    """Rasterize letters/digits at 32px; expect >80% non-empty ink bitmaps."""
    try:
        import freetype
    except ImportError:
        return {
            "passed": True,
            "skipped": True,
            "details": "freetype-py not installed on this interpreter; skipped",
            "ink_glyphs": 0,
            "empty_glyphs": 0,
        }

    result: Dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "details": "",
        "ink_glyphs": 0,
        "empty_glyphs": 0,
    }
    try:
        face = freetype.Face(path)
        face.set_pixel_sizes(0, 32)
        ink_chars = sorted({c for c in sample_text if c.isalnum()})
        empty = 0
        for ch in ink_chars:
            try:
                face.load_char(ch, freetype.FT_LOAD_RENDER)
                bitmap = face.glyph.bitmap
                if bitmap.width == 0 or bitmap.rows == 0:
                    empty += 1
            except Exception:  # noqa: BLE001 - unmapped/raster error counts empty
                empty += 1
        total = len(ink_chars)
        result["ink_glyphs"] = total
        result["empty_glyphs"] = empty
        non_empty_ratio = (total - empty) / total if total else 1.0
        result["passed"] = non_empty_ratio > 0.80
        result["details"] = f"{total - empty}/{total} ink chars rasterized non-empty at 32px"
    except Exception as exc:  # noqa: BLE001
        result["details"] = f"freetype exception: {exc}"
        result["passed"] = False
    return result


@dataclass
class ValidationReport:
    """Aggregated heavy-validation outcome (fast15.md final route)."""

    fonttools_ttf: Dict[str, Any] = field(default_factory=dict)
    fonttools_otf: Dict[str, Any] = field(default_factory=dict)
    harfbuzz: Dict[str, Any] = field(default_factory=dict)
    freetype: Dict[str, Any] = field(default_factory=dict)
    skipped_engines: List[str] = field(default_factory=list)
    passed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fonttools_ttf": self.fonttools_ttf,
            "fonttools_otf": self.fonttools_otf,
            "harfbuzz": self.harfbuzz,
            "freetype": self.freetype,
            "skipped_engines": list(self.skipped_engines),
            "passed": self.passed,
        }


def heavy_validation(
    model,
    ttf_path: str,
    otf_path: str,
    vietnamese: bool = False,
) -> ValidationReport:
    """One heavy validation route per fast15.md.

    FontTools structure checks run on BOTH binaries; HarfBuzz shaping and
    FreeType rasterization run on the TTF route only. Skipped optional
    engines are listed, never counted as failures.
    """
    corpus = list(CORPUS_BASE) + (list(CORPUS_VIETNAMESE) if vietnamese else [])
    report = ValidationReport()
    report.fonttools_ttf = fonttools_validate(ttf_path)
    report.fonttools_otf = fonttools_validate(otf_path)
    report.harfbuzz = harfbuzz_validate(ttf_path, corpus)
    report.freetype = freetype_validate(ttf_path, "".join(corpus))

    if report.harfbuzz.get("skipped"):
        report.skipped_engines.append("harfbuzz")
    if report.freetype.get("skipped"):
        report.skipped_engines.append("freetype")

    report.passed = all(
        part.get("passed", False)
        for part in (
            report.fonttools_ttf,
            report.fonttools_otf,
            report.harfbuzz,
            report.freetype,
        )
    )
    return report
