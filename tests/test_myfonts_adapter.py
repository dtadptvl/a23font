"""Offline tests for the MyFonts source adapter (M4.A2).

Covers fixture parsing (font-render-image, data-md5hash+name, bare fallback,
dedupe, zero-styles), SourceIdentity semantics, URL allowlist rejection, and
redirect-host guards (pure function + httpx.MockTransport).
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import security
from app.config import Config
from pipeline import source_myfonts
from pipeline.models import FontRequest, GlyphManifest, SourceIdentity

GOOD_URL = "https://www.myfonts.com/collections/postamp-grotesk-font-fontfabric"

FIXTURE_FONT_RENDER = """<!DOCTYPE html>
<html><head><title>Postamp Grotesk Font Family | MyFonts</title></head>
<body>
<div class="collection" data-collection-title="Postamp Grotesk">
  <font-render-image md5="0123456789abcdef0123456789abcdef" default="Postamp Grotesk Regular" src="/render/x"></font-render-image>
  <font-render-image class="lazy" src="/render/y" md5="FEDCBA9876543210FEDCBA9876543210" default="Postamp Grotesk Bold"></font-render-image>
</div>
<script>itemDataLayer.brand = 'FontFabric';</script>
</body></html>"""

FIXTURE_MD5HASH_NAMED = """<!DOCTYPE html>
<html><head><title>Postamp Grotesk | MyFonts</title></head>
<body>
<div class="style_row">
  <div class="font_info_name" data-md5hash="aaaa0000bbbb1111cccc2222dddd3333">Postamp Grotesk Light</div>
</div>
<div class="style_row">
  <span class="font_info_name" data-md5hash="1111222233334444555566667777aaaa">Postamp Grotesk Italic</span>
</div>
<script type="application/ld+json">{"@type": "Product", "name": "Postamp Grotesk", "brand": {"name": "FontFabric"}}</script>
</body></html>"""

FIXTURE_BARE_MD5HASH = """<!DOCTYPE html>
<html><head><title>Some Family | MyFonts</title></head>
<body>
<ul class="results">
  <li data-md5hash="0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"></li>
  <li data-md5hash="1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e"></li>
</ul>
</body></html>"""

FIXTURE_DEDUPE = """<!DOCTYPE html>
<html><head><title>Dedupe Family | MyFonts</title></head><body>
<font-render-image md5="abcd1234abcd1234abcd1234abcd1234"></font-render-image>
<div data-md5hash="ABCD1234ABCD1234ABCD1234ABCD1234">Postamp Grotesk Medium</div>
<div data-md5hash="abcd1234abcd1234abcd1234abcd1234"></div>
</body></html>"""

FIXTURE_NO_STYLES = "<html><head><title>Just a challenge</title></head><body><p>Verify you are human.</p></body></html>"


def offline_cfg(tmp_path) -> Config:
    return Config(data_root=tmp_path / "data", browser_enabled=False)


# ---------------------------------------------------------------------------
# (a) font-render-image pattern
# ---------------------------------------------------------------------------

def test_parse_font_render_image_pattern():
    request = source_myfonts.parse_collection(FIXTURE_FONT_RENDER, GOOD_URL)
    assert isinstance(request, FontRequest)
    assert request.family_name == "Postamp Grotesk"
    assert request.foundry == "FontFabric"
    assert len(request.styles) == 2
    first, second = request.styles
    assert first.name == "Postamp Grotesk Regular"
    assert first.weight == "Regular"
    assert first.identity == SourceIdentity.from_md5("0123456789abcdef0123456789abcdef")
    assert first.identity.kind == "md5"
    assert first.identity.confidence == "exact"
    assert first.identity.stable_id == "md5:0123456789abcdef0123456789abcdef"
    assert first.page_url == GOOD_URL
    # shuffled attribute order + uppercase md5 tolerated
    assert second.name == "Postamp Grotesk Bold"
    assert second.weight == "Bold"
    assert second.identity.value == "fedcba9876543210fedcba9876543210"
    assert any("font-render-image" in note for note in request.notes)
    json.dumps(request.to_dict())  # to_dict must be JSON-serializable


# ---------------------------------------------------------------------------
# (b) data-md5hash + nearby name pattern
# ---------------------------------------------------------------------------

def test_parse_md5hash_with_nearby_name():
    request = source_myfonts.parse_collection(FIXTURE_MD5HASH_NAMED, GOOD_URL)
    assert request.family_name == "Postamp Grotesk"  # via JSON-LD fallback
    assert request.foundry == "FontFabric"  # via JSON-LD brand
    assert [(s.name, s.weight) for s in request.styles] == [
        ("Postamp Grotesk Light", "Light"),
        ("Postamp Grotesk Italic", None),  # Italic is not a weight
    ]
    assert request.styles[0].identity.value == "aaaa0000bbbb1111cccc2222dddd3333"
    assert request.styles[1].identity.value == "1111222233334444555566667777aaaa"
    assert all(s.identity.kind == "md5" for s in request.styles)


# ---------------------------------------------------------------------------
# (c) bare data-md5hash fallback with synthesized names
# ---------------------------------------------------------------------------

def test_parse_bare_md5hash_synthesizes_names():
    request = source_myfonts.parse_collection(FIXTURE_BARE_MD5HASH, GOOD_URL)
    assert request.family_name == "Some Family"  # via <title> suffix stripping
    assert [s.name for s in request.styles] == ["Style 1", "Style 2"]
    assert all(s.metadata.get("synthesized_name") for s in request.styles)
    assert any("synthesized names" in note for note in request.notes)
    assert request.styles[0].identity.value == "0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"


# ---------------------------------------------------------------------------
# (d) dedupe by md5 preserving order, upgrading missing names
# ---------------------------------------------------------------------------

def test_parse_dedupes_by_md5_preserving_order():
    request = source_myfonts.parse_collection(FIXTURE_DEDUPE, GOOD_URL)
    assert len(request.styles) == 1
    style = request.styles[0]
    assert style.identity.value == "abcd1234abcd1234abcd1234abcd1234"
    assert style.name == "Postamp Grotesk Medium"  # upgraded from bare first occurrence
    assert style.weight == "Medium"


# ---------------------------------------------------------------------------
# (e) zero styles raises ValueError
# ---------------------------------------------------------------------------

def test_parse_zero_styles_raises():
    with pytest.raises(ValueError, match="no font styles found"):
        source_myfonts.parse_collection(FIXTURE_NO_STYLES, GOOD_URL)
    with pytest.raises(ValueError, match="empty page"):
        source_myfonts.parse_collection("", GOOD_URL)


# ---------------------------------------------------------------------------
# (f) SourceIdentity semantics
# ---------------------------------------------------------------------------

def test_source_identity_from_md5_exact():
    identity = SourceIdentity.from_md5("ABCDEF0123456789abcdef0123456789")
    assert identity.kind == "md5"
    assert identity.value == "abcdef0123456789abcdef0123456789"
    assert identity.confidence == "exact"
    assert identity.raw_ref == "ABCDEF0123456789abcdef0123456789"
    assert identity.stable_id == "md5:abcdef0123456789abcdef0123456789"


def test_source_identity_from_md5_rejects_bad_input():
    for bad in ("xyz", "0123456789abcdef0123456789abcde", "g" * 32, "", 12345):
        with pytest.raises(ValueError):
            SourceIdentity.from_md5(bad)


def test_source_identity_fallback_stable_and_deterministic():
    one = SourceIdentity.fallback("Cafe  Grotesk", "Bold ", "FOUNDRY")
    two = SourceIdentity.fallback("cafe grotesk", " bold", "foundry")
    assert one.kind == "fallback"
    assert one.confidence == "stable"
    assert one.raw_ref is None
    assert len(one.value) == 32
    assert all(ch in "0123456789abcdef" for ch in one.value)
    assert one.value == two.value  # NFKD + casefold + whitespace collapse
    assert one.stable_id == f"fallback:{one.value}"
    other = SourceIdentity.fallback("Cafe Grotesk", "Light", "FOUNDRY")
    assert other.value != one.value


def test_glyph_manifest_construction():
    manifest = GlyphManifest(
        md5="0" * 32, total_glyphs=2, unicode_coverage=[65, 66], pages=1, stop_reason="empty_result"
    )
    assert manifest.glyphs == []
    assert manifest.stop_reason == "empty_result"


# ---------------------------------------------------------------------------
# (g) URL rejection (adapter-level; reuses app.security)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("url", "expected_code"),
    [
        ("http://www.myfonts.com/collections/foo", "bad_scheme"),
        ("https://fonts.google.com/collections/foo", "bad_host"),
        ("https://evilwww.myfonts.com/collections/foo", "bad_host"),
        ("https://10.0.0.1/collections/foo", "bad_host"),
        ("https://localhost/fonts/foo", "bad_host"),
        ("https://www.myfonts.com/download/foo", "bad_path"),
    ],
)
def test_resolve_rejects_invalid_urls(url, expected_code, tmp_path):
    with pytest.raises(security.SourceError) as err:
        asyncio.run(source_myfonts.resolve(url, offline_cfg(tmp_path)))
    assert err.value.code == expected_code


# ---------------------------------------------------------------------------
# Redirect-host validation (pure function)
# ---------------------------------------------------------------------------

def test_validate_redirect_target_rejects_foreign_hosts():
    current = "https://www.myfonts.com/collections/foo"
    for location in (
        "https://evil.example.com/",
        "//evil.example.com/x",
        "https://myfonts.com.evil.example.com/collections/x",
        "https://myfonts.com/collections/x",  # apex is not the allowlisted host
        "http://www.myfonts.com/collections/x",  # scheme downgrade
        "https://user@www.myfonts.com/collections/x",  # userinfo
        "",
    ):
        with pytest.raises(security.SourceError):
            source_myfonts.validate_redirect_target(location, current)


def test_validate_redirect_target_accepts_allowlisted():
    current = "https://www.myfonts.com/collections/foo"
    assert (
        source_myfonts.validate_redirect_target("/collections/bar?x=1", current)
        == "https://www.myfonts.com/collections/bar?x=1"
    )
    assert (
        source_myfonts.validate_redirect_target("https://www.myfonts.com/fonts/fam", current)
        == "https://www.myfonts.com/fonts/fam"
    )


# ---------------------------------------------------------------------------
# fetch_page redirect handling via httpx.MockTransport
# ---------------------------------------------------------------------------

def test_fetch_page_rejects_redirect_to_foreign_host(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "resolve_host_public", lambda host: ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example.com/steal"})

    with pytest.raises(security.SourceError) as err:
        asyncio.run(
            source_myfonts.fetch_page(
                GOOD_URL, offline_cfg(tmp_path), transport=httpx.MockTransport(handler)
            )
        )
    assert err.value.code == "bad_host"


def test_fetch_page_follows_allowlisted_redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "resolve_host_public", lambda host: ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/collections/start":
            return httpx.Response(302, headers={"Location": "/collections/real"})
        if request.url.path == "/collections/real":
            return httpx.Response(
                200,
                text=FIXTURE_FONT_RENDER,
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        return httpx.Response(404, text="not found")

    html_text, method = asyncio.run(
        source_myfonts.fetch_page(
            "https://www.myfonts.com/collections/start",
            offline_cfg(tmp_path),
            transport=httpx.MockTransport(handler),
        )
    )
    assert method == "http"
    assert "data-collection-title" in html_text


def test_fetch_page_http_error_without_fallback_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "resolve_host_public", lambda host: ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<html>challenge</html>")

    with pytest.raises(security.SourceError) as err:
        asyncio.run(
            source_myfonts.fetch_page(
                GOOD_URL, offline_cfg(tmp_path), transport=httpx.MockTransport(handler)
            )
        )
    assert err.value.code == "fetch_failed"
    assert "403" in str(err.value)


def test_fetch_page_returns_markerless_200_body_best_effort(tmp_path, monkeypatch):
    # No markers + browser disabled: body is returned; parse_collection is the
    # component that raises a clear ValueError downstream.
    monkeypatch.setattr(security, "resolve_host_public", lambda host: ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=FIXTURE_NO_STYLES)

    html_text, method = asyncio.run(
        source_myfonts.fetch_page(
            GOOD_URL, offline_cfg(tmp_path), transport=httpx.MockTransport(handler)
        )
    )
    assert method == "http"
    with pytest.raises(ValueError, match="no font styles found"):
        source_myfonts.parse_collection(html_text, GOOD_URL)


def test_resolve_attaches_request_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "resolve_host_public", lambda host: ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=FIXTURE_FONT_RENDER)

    request = asyncio.run(
        source_myfonts.resolve(
            "https://myfonts.com/collections/postamp-grotesk-font-fontfabric?utm=x#top",
            offline_cfg(tmp_path),
            vietnamese=True,
            transport=httpx.MockTransport(handler),
        )
    )
    assert request.source_url == GOOD_URL  # canonicalized by app.security
    assert request.vietnamese is True
    assert request.fetch_method == "http"
    assert request.fetched_at.endswith("+00:00")
    assert len(request.styles) == 2
    assert any(note.startswith("fetch_method=") for note in request.notes)


def test_find_chromium_disabled_returns_none(tmp_path):
    assert source_myfonts.find_chromium(offline_cfg(tmp_path)) is None
