"""Restricted live HTTP client for the A23Font production pipeline (M5).

The worker's live route talks to two external surfaces:

  * www.myfonts.com   - source resolution; pipeline.source_myfonts owns that
                        client (URL allowlist + SSRF guard + redirect guards).
  * sig.monotype.com  - gmap discovery pages + glyph raster fetches; this
                        module owns that client.

Safety/honesty rules enforced here:
  * every URL (initial and each redirect hop) must be https and its host must
    be on the allowlist: sig.monotype.com plus any hosts configured via
    A23FONT_EXTRA_SOURCE_HOSTS (comma-separated; covers a CDN that the raster
    endpoint may redirect to);
  * transient failures (DNS/connect/read flaps on the mobile network, and
    HTTP 429/502/503/504) are retried a bounded number of times with backoff;
  * outgoing sockets bind to IPv4 (the phone's IPv6 egress is blackholed
    while DNS may sort AAAA first - see deploy/a23/Dockerfile.a23);
  * non-200 raster payloads surface as None/ LiveHttpError, never as data.
"""
from __future__ import annotations

import asyncio
from typing import Any, FrozenSet, Iterable, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from pipeline import discovery

__all__ = ["LiveHttpError", "RestrictedClient", "live_raster_hosts"]

ATTEMPTS = 3
RETRY_BACKOFF_S = 0.75
MAX_REDIRECTS = 5
TIMEOUT = httpx.Timeout(connect=20.0, read=90.0, write=30.0, pool=15.0)
_RETRY_STATUSES = frozenset({429, 502, 503, 504})


class LiveHttpError(RuntimeError):
    """Hard live-fetch failure with a machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def live_raster_hosts(cfg: Any) -> FrozenSet[str]:
    """Allowlist for live pipeline fetches.

    The raster/discovery host (sig.monotype.com) plus any extra source hosts
    configured via A23FONT_EXTRA_SOURCE_HOSTS (comma/semicolon separated,
    case-insensitive). Extra hosts exist for CDN hosts the raster endpoint
    may redirect to; keep the list minimal.
    """
    hosts = {discovery.RASTER_HOST}
    extra = str(getattr(cfg, "extra_source_hosts", "") or "")
    for chunk in extra.replace(";", ",").split(","):
        host = chunk.strip().lower()
        if host:
            hosts.add(host)
    return frozenset(hosts)


class RestrictedClient:
    """Async GET-only client: allowlist + retries + IPv4 + redirect guards."""

    def __init__(
        self,
        hosts: Iterable[str],
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        attempts: int = ATTEMPTS,
        timeout: Optional[httpx.Timeout] = None,
    ) -> None:
        self.hosts = frozenset(str(h).lower() for h in hosts)
        if not self.hosts:
            raise ValueError("RestrictedClient requires at least one allowed host")
        self.attempts = max(1, int(attempts))
        if transport is None:
            # local_address binds outgoing sockets to IPv4 only: on the phone
            # IPv6 egress is blackholed while DNS may sort AAAA first.
            transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout or TIMEOUT,
            follow_redirects=False,
        )

    async def __aenter__(self) -> "RestrictedClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._client.aclose()

    # -- guards ---------------------------------------------------------------

    def _check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme.lower() != "https":
            raise LiveHttpError("LIVE_BAD_SCHEME", f"non-https live url: {url!r}")
        if parts.username is not None or parts.password is not None:
            raise LiveHttpError("LIVE_BAD_URL", f"userinfo in live url: {url!r}")
        host = (parts.hostname or "").lower()
        if host not in self.hosts:
            raise LiveHttpError(
                "LIVE_HOST_NOT_ALLOWED",
                f"host not on live allowlist: {host or '<empty>'}",
            )

    # -- fetching -------------------------------------------------------------

    async def _get_once(self, url: str) -> httpx.Response:
        """One GET with bounded retries on transient failures."""
        last_error = "unknown"
        for attempt in range(1, self.attempts + 1):
            try:
                response = await self._client.get(url)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.attempts:
                    await asyncio.sleep(RETRY_BACKOFF_S * attempt)
                    continue
                raise LiveHttpError(
                    "LIVE_NETWORK",
                    f"GET {url} failed after {attempt} attempts: {last_error}",
                ) from exc
            if response.status_code in _RETRY_STATUSES and attempt < self.attempts:
                last_error = f"HTTP {response.status_code}"
                await response.aclose()
                await asyncio.sleep(RETRY_BACKOFF_S * attempt)
                continue
            return response
        raise LiveHttpError("LIVE_NETWORK", f"GET {url} failed: {last_error}")

    async def _get_validated(self, url: str) -> httpx.Response:
        """GET with hop-by-hop redirect validation inside the allowlist."""
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            self._check_url(current)
            response = await self._get_once(current)
            if not response.is_redirect:
                return response
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise LiveHttpError(
                    "LIVE_BAD_REDIRECT", f"redirect without Location from {current}"
                )
            current = urljoin(current, location.strip())
        raise LiveHttpError(
            "LIVE_TOO_MANY_REDIRECTS", f"more than {MAX_REDIRECTS} redirects for {url}"
        )

    async def get_json(self, url: str) -> Any:
        """GET one allowlisted URL and parse JSON; non-200 is a hard error."""
        response = await self._get_validated(url)
        try:
            if response.status_code != 200:
                raise LiveHttpError(
                    "LIVE_HTTP_ERROR", f"HTTP {response.status_code} for {url}"
                )
            return response.json()
        finally:
            await response.aclose()

    async def get_bytes(self, url: str) -> Optional[bytes]:
        """GET one allowlisted URL; non-200 (after retries) returns None."""
        response = await self._get_validated(url)
        try:
            if response.status_code != 200:
                return None
            return response.content
        finally:
            await response.aclose()
