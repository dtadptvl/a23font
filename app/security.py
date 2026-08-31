"""Source URL validation and SSRF protection."""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import List
from urllib.parse import urlsplit

ALLOWED_HOST = "www.myfonts.com"
_ALLOWED_HOSTS = ("myfonts.com", "www.myfonts.com")
ALLOWED_PREFIXES = ("/collections/", "/fonts/")
MAX_URL_LEN = 2048


class SourceError(ValueError):
    """Raised when a source URL or host fails validation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NormalizedUrl:
    """Canonical form of a validated source URL."""

    url: str
    path: str
    kind: str


NormalizedUrl = dataclass(frozen=True)(NormalizedUrl)


def validate_source_url(raw: str) -> NormalizedUrl:
    """Validate and canonicalize a MyFonts source URL.

    Raises SourceError with a machine-readable code on any violation.
    """
    if not isinstance(raw, str):
        raise SourceError("bad_format", "source URL must be a string")
    text = raw.strip()
    if not text:
        raise SourceError("bad_format", "source URL is empty")
    if len(text) > MAX_URL_LEN:
        raise SourceError("too_long", f"source URL exceeds {MAX_URL_LEN} characters")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise SourceError("bad_format", f"unparseable URL: {exc}") from exc
    if parts.scheme.lower() != "https":
        raise SourceError("bad_scheme", "only https URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise SourceError("bad_format", "userinfo in URL is not allowed")
    try:
        port = parts.port
    except ValueError as exc:
        raise SourceError("bad_format", f"invalid port in URL: {exc}") from exc
    if port is not None:
        raise SourceError("bad_format", "explicit port in URL is not allowed")
    host = (parts.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise SourceError("bad_host", f"host not allowed: {host or '<empty>'}")
    path = parts.path
    for segment in path.split("/"):
        if segment == "..":
            raise SourceError("bad_path", "path traversal segments are not allowed")
    kind = None
    for prefix in ALLOWED_PREFIXES:
        if path.startswith(prefix):
            remainder = path[len(prefix):]
            segments = [seg for seg in remainder.split("/") if seg]
            if not segments:
                raise SourceError("bad_path", "path has no resource segment after prefix")
            kind = "collection" if prefix == "/collections/" else "family"
            break
    if kind is None:
        raise SourceError("bad_path", f"path must start with one of {', '.join(ALLOWED_PREFIXES)}")
    canonical = f"https://{ALLOWED_HOST}{path}"
    return NormalizedUrl(url=canonical, path=path, kind=kind)


def is_public_address(ip) -> bool:
    """Return True when the ipaddress object is a globally routable address."""
    return not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def resolve_host_public(host: str) -> List[str]:
    """Resolve host for TCP/443 and require every resolved address to be public."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise SourceError("dns_failure", f"DNS resolution failed for {host}: {exc}") from exc
    addresses: List[str] = []
    for info in infos:
        address = str(info[4][0]).split("%")[0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise SourceError("unsafe_address", f"no addresses resolved for {host}")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not is_public_address(ip):
            raise SourceError("unsafe_address", f"non-public address {address} resolved for {host}")
    return addresses
