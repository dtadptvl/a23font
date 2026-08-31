"""Offline tests for pipeline.discovery (M3.A4).

Scripted async fetch_page sequences exercise every stop rule
(empty_layout, repeated_signature, no_new_glyphs, short_page, max_pages,
error), defensive entry parsing, the URL builders, and GlyphManifest
population. No network access: discovery only uses the injected callable.
"""
from __future__ import annotations

import asyncio

from pipeline import discovery
from pipeline.models import GlyphManifest

MD5 = "0123456789abcdef0123456789abcdef"


def scripted_fetch(script):
    """Build an async fetch_page that replays script items (or raises)."""
    calls = []

    async def fetch(url):
        calls.append(url)
        index = len(calls) - 1
        assert index < len(script), f"unexpected fetch beyond script (call {index + 1})"
        item = script[index]
        if isinstance(item, BaseException):
            raise item
        return item

    return fetch, calls


def layout_block(start, count, key_offset=0):
    """count layout entries: gid g<start+i>, codepoint 65+start+i."""
    layout = {}
    for i in range(count):
        layout[str(key_offset + i)] = {
            "glyph": f"g{start + i}",
            "codePoint": 65 + start + i,
        }
    return layout


# ---------------------------------------------------------------------------
# (a) short final page
# ---------------------------------------------------------------------------

def test_short_page_stops_and_collects():
    script = [
        {"layout": layout_block(0, 100), "image": "img1"},
        {"layout": layout_block(100, 37), "image": "img2"},
    ]
    fetch, calls = scripted_fetch(script)
    manifest = asyncio.run(discovery.discover_glyphs(fetch, MD5, gpp=100))
    assert isinstance(manifest, GlyphManifest)
    assert manifest.md5 == MD5
    assert manifest.stop_reason == "short_page"
    assert manifest.total_glyphs == 137
    assert manifest.pages == 2
    assert len(manifest.entries) == 137
    assert manifest.unicode_coverage == sorted({65 + i for i in range(137)})
    assert manifest.unicode_coverage == sorted(manifest.unicode_coverage)
    assert "acs_p=1" in calls[0] and "acs_p=2" in calls[1]
    assert "acs_gpp=100" in calls[0]


# ---------------------------------------------------------------------------
# (b) empty layout
# ---------------------------------------------------------------------------

def test_empty_layout_stops():
    fetch, _ = scripted_fetch([{"layout": {}, "image": ""}])
    manifest = asyncio.run(discovery.discover_glyphs(fetch, MD5))
    assert manifest.stop_reason == "empty_layout"
    assert manifest.total_glyphs == 0
    assert manifest.pages == 1
    assert manifest.unicode_coverage == []
    assert manifest.entries == []


def test_missing_layout_is_empty():
    fetch, _ = scripted_fetch([{"image": "only an image"}])
    manifest = asyncio.run(discovery.discover_glyphs(fetch, MD5))
    assert manifest.stop_reason == "empty_layout"
    assert manifest.total_glyphs == 0


# ---------------------------------------------------------------------------
# (c) repeated page signature
# ---------------------------------------------------------------------------

def test_repeated_signature_stops():
    page = {"layout": layout_block(0, 4), "image": "img"}
    fetch, _ = scripted_fetch([page, dict(page)])
    manifest = asyncio.run(discovery.discover_glyphs(fetch, MD5, gpp=4))
    assert manifest.stop_reason == "repeated_signature"
    assert manifest.pages == 2
    assert manifest.total_glyphs == 4  # second page contributed nothing


# ---------------------------------------------------------------------------
# (d) no new glyphs (same glyphs, different page shape/signature)
# ---------------------------------------------------------------------------

def test_no_new_glyphs_stops():
    page1 = {"layout": layout_block(0, 4, key_offset=0), "image": "imgA"}
    page2 = {"layout": layout_block(0, 4, key_offset=100), "image": "imgB"}
    fetch, _ = scripted_fetch([page1, page2])
    manifest = asyncio.run(discovery.discover_glyphs(fetch, MD5, gpp=4))
    assert manifest.stop_reason == "no_new_glyphs"
    assert manifest.pages == 2
    assert manifest.total_glyphs == 4


# ---------------------------------------------------------------------------
# (e) fetch errors
# ---------------------------------------------------------------------------

def test_fetch_exception_is_error():
    fetch, _ = scripted_fetch([RuntimeError("boom")])
    manifest = asyncio.run(discovery.discover_glyphs(fetch, MD5))
    assert manifest.stop_reason == "error"
    assert manifest.pages == 0
    assert manifest.total_glyphs == 0
    assert any("boom" in note for note in manifest.notes)


def test_non_dict_payload_is_error():
    fetch, _ = scripted_fetch(["<html>garbage</html>"])
    manifest = asyncio.run(discovery.discover_glyphs(fetch, MD5))
    assert manifest.stop_reason == "error"
    assert manifest.pages == 0
    assert any("str" in note for note in manifest.notes)


# ---------------------------------------------------------------------------
# (f) max_pages budget
# ---------------------------------------------------------------------------

def test_max_pages_budget():
    script = [
        {"layout": layout_block(i * 3, 3), "image": f"img{i}"} for i in range(3)
    ]
    fetch, calls = scripted_fetch(script)
    manifest = asyncio.run(discovery.discover_glyphs(fetch, MD5, gpp=3, max_pages=3))
    assert manifest.stop_reason == "max_pages"
    assert manifest.pages == 3
    assert manifest.total_glyphs == 9
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# defensive entry parsing
# ---------------------------------------------------------------------------

def test_defensive_entry_parsing():
    layout = {
        "3": {"glyph": "Aacute", "codePoint": 193, "advance": 600},
        "7": {
            "codepoint": 66,  # lowercase variant
            "advance": 550,
            "bold": True,
            "bitmap": "x" * 500,  # image-like large value: skipped
            "shape": {"deep": 1},  # non-scalar: skipped
        },
        "9": {"unicode": "67"},  # no glyph id: key used; str cp coerced
        "junk": "not-a-dict",  # skipped entirely
    }
    fetch, _ = scripted_fetch([{"layout": layout, "image": "img"}])
    manifest = asyncio.run(discovery.discover_glyphs(fetch, MD5))
    assert manifest.stop_reason == "short_page"
    entries = {e["gid"]: e for e in manifest.entries}
    assert set(entries) == {"Aacute", "7", "9"}
    assert entries["Aacute"]["cp"] == 193
    assert entries["7"]["cp"] == 66
    assert entries["9"]["gid"] == "9"  # layout key becomes gid
    assert entries["9"]["cp"] == 67
    meta = entries["7"]["meta"]
    assert meta["advance"] == 550
    assert meta["bold"] is True
    assert "bitmap" not in meta
    assert "shape" not in meta
    assert "codepoint" not in meta and "glyph" not in meta
    assert manifest.unicode_coverage == [66, 67, 193]


def test_coverage_excludes_nonpositive_codepoints():
    layout = {
        "0": {"glyph": "g0", "codePoint": 0},
        "1": {"glyph": "g1", "codePoint": -4},
        "2": {"glyph": "g2", "codePoint": 65},
        "3": {"glyph": "g3"},  # no codepoint field at all -> cp 0
    }
    fetch, _ = scripted_fetch([{"layout": layout, "image": ""}])
    manifest = asyncio.run(discovery.discover_glyphs(fetch, MD5))
    assert manifest.total_glyphs == 4
    assert manifest.unicode_coverage == [65]


# ---------------------------------------------------------------------------
# page delay
# ---------------------------------------------------------------------------

def test_page_delay_sleeps_between_full_pages(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(discovery.asyncio, "sleep", fake_sleep)
    script = [
        {"layout": layout_block(0, 2), "image": "i0"},
        {"layout": layout_block(2, 2), "image": "i1"},
        {"layout": {}, "image": ""},
    ]
    fetch, _ = scripted_fetch(script)
    manifest = asyncio.run(
        discovery.discover_glyphs(fetch, MD5, gpp=2, page_delay_s=0.01)
    )
    assert manifest.stop_reason == "empty_layout"
    assert sleeps == [0.01, 0.01]  # no sleep after the breaking page


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------

def test_gmap_page_url_exact():
    url = discovery.gmap_page_url(MD5, 3)
    assert url == (
        f"https://sig.monotype.com/render/105/font/{MD5}"
        "?rbe=gmap&acs_pt=120&acs_w=1500&acs_l=1&acs_ar=0&acs_p=3&acs_gpp=100"
    )
    custom = discovery.gmap_page_url(MD5, 1, pt=96, width=1024, gpp=50)
    assert "acs_pt=96" in custom and "acs_w=1024" in custom and "acs_gpp=50" in custom


def test_glyph_raster_url_encoding_and_defaults():
    url = discovery.glyph_raster_url(MD5, "A", 64)
    assert f"/render/105/font/{MD5}?" in url
    assert "rt=A" in url
    assert "pt=64" in url
    assert "w=128" in url  # default width = 2 * size
    assert "sc=2" in url  # default scale
    assert "fg=000000" in url and "bg=FFFFFF" in url
    assert "render_mode=new" in url

    vn = discovery.glyph_raster_url(MD5, "\u1eef", 100, width_px=400, scale=4)
    assert "rt=%E1%BB%AF" in vn  # URL-encoded UTF-8 for a Vietnamese char
    assert "pt=100" in vn and "w=400" in vn and "sc=4" in vn

    spaced = discovery.glyph_raster_url(MD5, "A B", 50)
    assert "rt=A%20B" in spaced


def test_discovery_module_has_no_network_clients():
    # injected-fetch design: no HTTP/socket client libraries are imported
    import inspect

    source = inspect.getsource(discovery)
    assert "import httpx" not in source
    assert "import requests" not in source
    assert "import aiohttp" not in source
    assert "socket" not in source