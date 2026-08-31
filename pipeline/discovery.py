"""Glyph discovery against the gmap layout endpoint (fast15.md, M3.A4).

Pure URL builders + an injected-fetch pagination loop. This module performs
no network I/O itself: the caller supplies ``fetch_page``, an async callable
(url) -> dict returning the already-parsed gmap JSON for one page.

Stop rules: empty_layout, repeated_signature, no_new_glyphs, short_page,
max_pages, error (fetch exception or non-dict payload).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

from pipeline.models import GlyphManifest

__all__ = [
    "RASTER_HOST",
    "DEFAULT_PT",
    "DEFAULT_WIDTH",
    "DEFAULT_GPP",
    "DEFAULT_MAX_PAGES",
    "gmap_page_url",
    "glyph_raster_url",
    "discover_glyphs",
]

RASTER_HOST = "sig.monotype.com"

DEFAULT_PT = 120
DEFAULT_WIDTH = 1500
DEFAULT_GPP = 100
DEFAULT_MAX_PAGES = 40

# Scalar meta values longer than this look like embedded image payloads; skip.
META_MAX_STR_LEN = 128
_ID_FIELDS = ("glyph", "gid")
_CP_FIELDS = ("codePoint", "codepoint", "unicode")


def gmap_page_url(
    md5: str,
    page: int,
    pt: int = DEFAULT_PT,
    width: int = DEFAULT_WIDTH,
    gpp: int = DEFAULT_GPP,
) -> str:
    """gmap layout page URL: glyph grid metadata for one page of the atlas."""
    return (
        f"https://{RASTER_HOST}/render/105/font/{md5}"
        f"?rbe=gmap&acs_pt={pt}&acs_w={width}&acs_l=1&acs_ar=0"
        f"&acs_p={page}&acs_gpp={gpp}"
    )


def glyph_raster_url(
    md5: str,
    text: str,
    size_px: int,
    width_px: Optional[int] = None,
    scale: int = 2,
    fg: str = "000000",
    bg: str = "FFFFFF",
) -> str:
    """Single-glyph raster URL; the text is URL-encoded, w defaults to 2*size."""
    if width_px is None:
        width_px = int(size_px) * 2
    return (
        f"https://{RASTER_HOST}/render/105/font/{md5}"
        f"?rbe=raster&render_mode=new&rt={quote(text, safe='')}"
        f"&pt={int(size_px)}&w={int(width_px)}&sc={int(scale)}&fg={fg}&bg={bg}"
    )


def _page_signature(layout: Dict[str, Any], image: str) -> str:
    """sha256 over (sorted layout keys, image prefix) to detect page repeats."""
    payload = json.dumps(sorted(layout.keys())) + str(image)[:200]
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_layout_entries(layout: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Defensively turn one gmap layout dict into gid/cp/meta entry dicts."""
    entries: List[Dict[str, Any]] = []
    for key, value in layout.items():
        if not isinstance(value, dict):
            continue
        gid = str(value.get("glyph") or value.get("gid") or key)
        cp_raw = value.get("codePoint") or value.get("codepoint") or value.get("unicode") or 0
        try:
            cp = int(cp_raw)
        except (TypeError, ValueError):
            cp = 0
        meta: Dict[str, Any] = {}
        for kk, vv in value.items():
            if kk in _ID_FIELDS or kk in _CP_FIELDS:
                continue
            if vv is None or isinstance(vv, (bool, int, float)):
                meta[kk] = vv
            elif isinstance(vv, str) and len(vv) <= META_MAX_STR_LEN:
                meta[kk] = vv
            # nested containers and long strings (image-like) are skipped
        entries.append({"gid": gid, "cp": cp, "meta": meta})
    return entries


async def discover_glyphs(
    fetch_page: Callable[[str], Awaitable[Any]],
    md5: str,
    *,
    pt: int = DEFAULT_PT,
    width: int = DEFAULT_WIDTH,
    gpp: int = DEFAULT_GPP,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_delay_s: float = 0.0,
) -> GlyphManifest:
    """Paginate gmap pages until a stop rule fires; collect glyph entries.

    fetch_page is an injected async callable (url) -> dict (already-parsed
    JSON), so this function never touches the network itself.
    """
    entries: List[Dict[str, Any]] = []
    notes: List[str] = []
    seen_keys: Set[Tuple[int, str]] = set()
    seen_signatures: Set[str] = set()
    pages = 0
    stop_reason = "max_pages"

    for page in range(1, int(max_pages) + 1):
        url = gmap_page_url(md5, page, pt=pt, width=width, gpp=gpp)
        try:
            data = await fetch_page(url)
        except Exception as exc:  # noqa: BLE001 - defensive stop rule
            stop_reason = "error"
            notes.append(f"page {page}: fetch failed: {type(exc).__name__}: {exc}")
            break
        if not isinstance(data, dict):
            stop_reason = "error"
            notes.append(f"page {page}: expected dict payload, got {type(data).__name__}")
            break
        pages += 1

        layout = data.get("layout") or {}
        if not isinstance(layout, dict):
            layout = {}
        image = data.get("image") or ""

        if not layout:
            stop_reason = "empty_layout"
            notes.append(f"page {page}: empty layout")
            break

        signature = _page_signature(layout, image)
        if signature in seen_signatures:
            stop_reason = "repeated_signature"
            notes.append(f"page {page}: repeated page signature {signature[:16]}")
            break
        seen_signatures.add(signature)

        new_entries: List[Dict[str, Any]] = []
        for entry in _parse_layout_entries(layout):
            key = (entry["cp"], entry["gid"])
            if key not in seen_keys:
                seen_keys.add(key)
                new_entries.append(entry)
        if not new_entries:
            stop_reason = "no_new_glyphs"
            notes.append(f"page {page}: no new glyphs")
            break
        entries.extend(new_entries)

        if len(layout) < gpp:
            stop_reason = "short_page"
            notes.append(f"page {page}: {len(layout)} entries < gpp {gpp}")
            break

        if page_delay_s > 0:
            await asyncio.sleep(page_delay_s)

    coverage = sorted({entry["cp"] for entry in entries if entry["cp"] > 0})
    return GlyphManifest(
        md5=md5,
        total_glyphs=len(entries),
        unicode_coverage=coverage,
        pages=pages,
        stop_reason=stop_reason,
        entries=entries,
        notes=notes,
    )