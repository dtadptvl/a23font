"""Focused tests for the live pipeline restricted HTTP client (T-007, M5).

Covers worker/live_http.py (host allowlist incl. configured extra source
hosts, hop-by-hop redirect validation, bounded retries on transient
failures) plus the worker's zero-glyph discovery guard (DISCOVERY_EMPTY)
inside the live production runner.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app import services
from app.config import Config
from app.db import open_db
from pipeline import discovery
from pipeline.models import FontRequest, GlyphManifest, SourceIdentity, StyleRef
from worker import main as worker_main
from worker.live_http import LiveHttpError, RestrictedClient, live_raster_hosts

SIG = discovery.RASTER_HOST  # "sig.monotype.com"
SOURCE_URL = "https://www.myfonts.com/collections/live-http-test"
MD5 = "cd" * 16


# ---------------------------------------------------------------------------
# allowlist composition
# ---------------------------------------------------------------------------

def test_live_raster_hosts_includes_raster_and_configured_extras():
    cfg = Config(
        data_root="./data",
        extra_source_hosts="Cdn.Example.com, other.example; third.example",
    )
    hosts = live_raster_hosts(cfg)
    assert SIG in hosts
    assert {"cdn.example.com", "other.example", "third.example"} <= hosts


def test_live_raster_hosts_default_is_raster_only():
    cfg = Config(data_root="./data")
    assert live_raster_hosts(cfg) == frozenset({SIG})


# ---------------------------------------------------------------------------
# mock transport fixtures
# ---------------------------------------------------------------------------

def _make_handler():
    calls = {"flaky": 0, "gateway": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"https://{SIG}/ok.png":
            return httpx.Response(200, content=b"PNGDATA")
        if url == f"https://{SIG}/gmap.json":
            return httpx.Response(200, json={"layout": {"a": {"glyph": "a"}}})
        if url == f"https://{SIG}/missing.png":
            return httpx.Response(404)
        if url == f"https://{SIG}/redir-cdn":
            return httpx.Response(
                302, headers={"location": "https://cdn.example.com/glyph.png"}
            )
        if url == "https://cdn.example.com/glyph.png":
            return httpx.Response(200, content=b"CDNDATA")
        if url == f"https://{SIG}/redir-evil":
            return httpx.Response(
                302, headers={"location": "https://evil.example.com/x.png"}
            )
        if url == f"https://{SIG}/flaky.png":
            calls["flaky"] += 1
            if calls["flaky"] < 3:
                raise httpx.ConnectError("simulated mobile DNS flap")
            return httpx.Response(200, content=b"FLAKYOK")
        if url == f"https://{SIG}/gateway.png":
            calls["gateway"] += 1
            if calls["gateway"] < 2:
                return httpx.Response(502)
            return httpx.Response(200, content=b"GATEWAYOK")
        return httpx.Response(500)

    return handler, calls


# ---------------------------------------------------------------------------
# allowlist + scheme enforcement
# ---------------------------------------------------------------------------

def test_host_allowlist_rejects_foreign_host():
    caught = {}

    async def run():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"x"))
        async with RestrictedClient({SIG}, transport=transport) as client:
            try:
                await client.get_bytes("https://evil.example.com/font.png")
            except LiveHttpError as exc:
                caught["code"] = exc.code

    asyncio.run(run())
    assert caught.get("code") == "LIVE_HOST_NOT_ALLOWED"


def test_non_https_url_rejected():
    caught = {}

    async def run():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"x"))
        async with RestrictedClient({SIG}, transport=transport) as client:
            try:
                await client.get_bytes(f"http://{SIG}/ok.png")
            except LiveHttpError as exc:
                caught["code"] = exc.code

    asyncio.run(run())
    assert caught.get("code") == "LIVE_BAD_SCHEME"


# ---------------------------------------------------------------------------
# fetch semantics
# ---------------------------------------------------------------------------

def test_get_bytes_200_returns_body_and_404_returns_none():
    handler, _ = _make_handler()

    async def run():
        async with RestrictedClient({SIG}, transport=httpx.MockTransport(handler)) as client:
            ok = await client.get_bytes(f"https://{SIG}/ok.png")
            missing = await client.get_bytes(f"https://{SIG}/missing.png")
            return ok, missing

    ok, missing = asyncio.run(run())
    assert ok == b"PNGDATA"
    assert missing is None


def test_get_json_parses_200_and_errors_on_non_200():
    handler, _ = _make_handler()
    caught = {}

    async def run():
        async with RestrictedClient({SIG}, transport=httpx.MockTransport(handler)) as client:
            payload = await client.get_json(f"https://{SIG}/gmap.json")
            try:
                await client.get_json(f"https://{SIG}/missing.png")
            except LiveHttpError as exc:
                caught["code"] = exc.code
            return payload

    payload = asyncio.run(run())
    assert payload == {"layout": {"a": {"glyph": "a"}}}
    assert caught.get("code") == "LIVE_HTTP_ERROR"


# ---------------------------------------------------------------------------
# redirect validation
# ---------------------------------------------------------------------------

def test_redirect_to_allowlisted_cdn_is_followed():
    handler, _ = _make_handler()

    async def run():
        hosts = {SIG, "cdn.example.com"}
        async with RestrictedClient(hosts, transport=httpx.MockTransport(handler)) as client:
            return await client.get_bytes(f"https://{SIG}/redir-cdn")

    assert asyncio.run(run()) == b"CDNDATA"


def test_redirect_to_non_allowlisted_host_is_rejected():
    handler, _ = _make_handler()
    caught = {}

    async def run():
        async with RestrictedClient({SIG}, transport=httpx.MockTransport(handler)) as client:
            try:
                await client.get_bytes(f"https://{SIG}/redir-evil")
            except LiveHttpError as exc:
                caught["code"] = exc.code

    asyncio.run(run())
    assert caught.get("code") == "LIVE_HOST_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# bounded retries for the flaky mobile network
# ---------------------------------------------------------------------------

def test_transient_transport_errors_retried_then_success(monkeypatch):
    monkeypatch.setattr("worker.live_http.RETRY_BACKOFF_S", 0.0)
    handler, calls = _make_handler()

    async def run():
        async with RestrictedClient({SIG}, transport=httpx.MockTransport(handler)) as client:
            return await client.get_bytes(f"https://{SIG}/flaky.png")

    assert asyncio.run(run()) == b"FLAKYOK"
    assert calls["flaky"] == 3  # two simulated flaps + one success


def test_gateway_502_retried_then_success(monkeypatch):
    monkeypatch.setattr("worker.live_http.RETRY_BACKOFF_S", 0.0)
    handler, calls = _make_handler()

    async def run():
        async with RestrictedClient({SIG}, transport=httpx.MockTransport(handler)) as client:
            return await client.get_bytes(f"https://{SIG}/gateway.png")

    assert asyncio.run(run()) == b"GATEWAYOK"
    assert calls["gateway"] == 2


def test_persistent_transport_error_raises_live_network(monkeypatch):
    monkeypatch.setattr("worker.live_http.RETRY_BACKOFF_S", 0.0)
    caught = {}

    def always_fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async def run():
        transport = httpx.MockTransport(always_fail)
        async with RestrictedClient({SIG}, transport=transport, attempts=2) as client:
            try:
                await client.get_bytes(f"https://{SIG}/ok.png")
            except LiveHttpError as exc:
                caught["code"] = exc.code

    asyncio.run(run())
    assert caught.get("code") == "LIVE_NETWORK"


# ---------------------------------------------------------------------------
# live runner honesty: zero-glyph discovery must fail the style
# ---------------------------------------------------------------------------

def test_live_runner_empty_manifest_fails_style_with_discovery_empty(
    monkeypatch, tmp_path
):
    cfg = Config(data_root=tmp_path / "data", pipeline_live=True)
    cfg.ensure_dirs()
    conn = open_db(cfg.data_root / "db" / "a23font.db")
    try:
        job = services.create_job(conn, SOURCE_URL, False, cfg)
        job_id = job["id"]
        assert worker_main.claim_next_queued(conn, "w-live", cfg) == job_id

        async def fake_resolve(url, cfg_, vietnamese):
            return FontRequest(
                source_url=url,
                family_name="LiveHttpTest",
                foundry=None,
                styles=[
                    StyleRef(
                        name="Live Regular",
                        weight=None,
                        identity=SourceIdentity.from_md5(MD5),
                        page_url=url,
                    )
                ],
                vietnamese=False,
                fetched_at="2026-09-01T00:00:00+00:00",
                fetch_method="http",
                notes=[],
            )

        async def fake_discover(fetch_page, md5, **kwargs):
            return GlyphManifest(
                md5=md5,
                total_glyphs=0,
                unicode_coverage=[],
                pages=0,
                stop_reason="error",
                entries=[],
                notes=["page 1: fetch failed: simulated mobile DNS flap"],
            )

        monkeypatch.setattr("pipeline.source_myfonts.resolve", fake_resolve)
        monkeypatch.setattr("pipeline.discovery.discover_glyphs", fake_discover)

        final = asyncio.run(
            worker_main.run_job(conn, cfg, services.get_job(conn, job_id), "w-live")
        )
        assert final == "FAILED"
        row = services.get_job(conn, job_id)
        assert row["status"] == "FAILED"
        assert row["error_code"] == "STYLES_FAILED"
        style_row = conn.execute(
            "SELECT * FROM style_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert style_row["status"] == "FAILED"
        assert style_row["error_code"] == "DISCOVERY_EMPTY"
        assert "simulated mobile DNS flap" in style_row["error_message"]
    finally:
        conn.close()
