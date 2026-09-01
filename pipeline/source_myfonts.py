"""MyFonts source adapter (milestone M4).

Resolves supported www.myfonts.com collection/family URLs into the
source-agnostic FontRequest model. Downstream pipeline stages must depend on
pipeline.models only, never on the HTML details handled here.

Security baseline is reused from app.security (URL allowlist/shape validation
plus SSRF address checks). Redirects are validated hop by hop against the
allowlist host; only https www.myfonts.com targets are followed.

Prior-art identity markers tolerated (multiple fallbacks, confidence in notes):
  * <font-render-image ... md5="<32hex>" ... default="<Style Name>" ...>
  * data-md5hash="<32hex>" attributes near style-name elements
  * bare data-md5hash lists (synthesized style names, lowest confidence)
"""
from __future__ import annotations

import asyncio
import html as html_module
import json
import logging
import re
import shutil
import subprocess
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import httpx

from app import security
from app.config import Config
from pipeline.models import FontRequest, SourceIdentity, StyleRef, utc_now_iso

log = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_HOST",
    "validate_redirect_target",
    "find_chromium",
    "fetch_page",
    "parse_collection",
    "resolve",
]

ALLOWED_HOST = security.ALLOWED_HOST

MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
# transient fetch failures (mobile DNS flaps) are retried once before the
# dump-dom fallback is considered
HTTP_ATTEMPTS = 2
MIN_RENDERED_CHARS = 2048
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
CHROMIUM_TIMEOUT_S = 90.0
CHROMIUM_PROBE_NAMES = ("chromium", "chromium-browser", "chrome", "google-chrome")
NAME_WINDOW = 600

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.myfonts.com/",
    "Origin": "https://www.myfonts.com",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

# Redirect-host validation failures must propagate; other fetch errors may fall
# back to dump-dom.
_GUARD_CODES = {"bad_host", "bad_scheme", "bad_format", "bad_redirect"}

_MD5_FULL = re.compile(r"[0-9a-f]{32}")
_MARKER = re.compile(
    r"font-render-image|data-md5hash|font_info_name|md5\s*=\s*[\"'][0-9a-fA-F]{32}",
    re.IGNORECASE,
)
_FONT_RENDER_TAG = re.compile(r"<font-render-image\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTR = re.compile(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_MD5HASH_ATTR = re.compile(
    r"data-md5hash\s*=\s*(?:\"([0-9a-fA-F]{32})\"|'([0-9a-fA-F]{32})')",
    re.IGNORECASE,
)
_COLLECTION_TITLE = re.compile(
    r"data-collection-title\s*=\s*(?:\"([^\"]+)\"|'([^']+)')", re.IGNORECASE
)
_TITLE_TAG = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)
_H1_TAG = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_JSONLD_BLOCK = re.compile(
    r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_BRAND_JS = re.compile(r"itemDataLayer\s*\.\s*brand\s*=\s*(?:\"([^\"]+)\"|'([^']+)')")
_BRAND_ATTR = re.compile(r"data-brand\s*=\s*(?:\"([^\"]+)\"|'([^']+)')", re.IGNORECASE)
_STYLE_LINK = re.compile(
    r"href\s*=\s*(?:\"(/fonts/[^\"#?]+)\"|'(/fonts/[^'#?]+)')", re.IGNORECASE
)
_NAME_TEXT_AFTER_MD5HASH = re.compile(
    r"data-md5hash\s*=\s*[\"'][0-9a-fA-F]{32}[\"'][^>]*>\s*([^<]{2,120})<",
    re.IGNORECASE,
)
_NEARBY_NAME_RES = (
    re.compile(r"data-style-name\s*=\s*(?:\"([^\"]{2,120})\"|'([^']{2,120})')", re.IGNORECASE),
    re.compile(r"data-name\s*=\s*(?:\"([^\"]{2,120})\"|'([^']{2,120})')", re.IGNORECASE),
    re.compile(
        r"class\s*=\s*[\"'][^\"']*font_info_name[^\"']*[\"'][^>]*>\s*([^<]{2,120})<",
        re.IGNORECASE,
    ),
    re.compile(r"aria-label\s*=\s*(?:\"([^\"]{2,120})\"|'([^']{2,120})')", re.IGNORECASE),
    re.compile(r"title\s*=\s*(?:\"([^\"]{2,120})\"|'([^']{2,120})')", re.IGNORECASE),
)

_WEIGHT_CANON = {
    "hairline": "Hairline",
    "thin": "Thin",
    "extralight": "ExtraLight",
    "ultralight": "UltraLight",
    "light": "Light",
    "regular": "Regular",
    "normal": "Regular",
    "book": "Book",
    "medium": "Medium",
    "semibold": "SemiBold",
    "demibold": "DemiBold",
    "bold": "Bold",
    "extrabold": "ExtraBold",
    "ultrabold": "UltraBold",
    "black": "Black",
    "heavy": "Heavy",
}


# ---------------------------------------------------------------------------
# Security guards (pure, unit-testable)
# ---------------------------------------------------------------------------

def validate_redirect_target(location: str, current_url: str) -> str:
    """Resolve a redirect Location against the current URL and enforce the allowlist.

    Returns the absolute target URL. Raises security.SourceError when the hop
    leaves https, adds userinfo, or targets a host other than www.myfonts.com.
    """
    if not isinstance(location, str) or not location.strip():
        raise security.SourceError("bad_redirect", "redirect with empty Location")
    target = urljoin(current_url, location.strip())
    parts = urlsplit(target)
    if parts.scheme.lower() != "https":
        raise security.SourceError(
            "bad_scheme", f"redirect to non-https target: {target!r}"
        )
    if parts.username is not None or parts.password is not None:
        raise security.SourceError(
            "bad_format", "redirect target contains userinfo"
        )
    host = (parts.hostname or "").lower()
    if host != ALLOWED_HOST:
        raise security.SourceError(
            "bad_host", f"redirect leaves allowlist host: {host or '<empty>'}"
        )
    return target


def find_chromium(cfg: Config) -> Optional[str]:
    """Locate a chromium-family binary via argv candidate names (never shell text)."""
    if not getattr(cfg, "browser_enabled", True):
        return None
    names: List[str] = []
    configured = str(getattr(cfg, "chromium_path", "") or "").strip()
    if configured:
        names.append(configured)
    for name in CHROMIUM_PROBE_NAMES:
        if name not in names:
            names.append(name)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

async def _http_get_chain(
    url: str, transport: Optional[httpx.AsyncBaseTransport]
) -> Tuple[int, str]:
    """GET with manual hop-by-hop redirect validation and a bounded body size.

    Returns (status, decoded_text) of the final response.
    """
    if transport is None:
        # IPv4-only sockets: on the A23 mobile network IPv6 egress is
        # blackholed while DNS may sort AAAA first (deploy/a23/Dockerfile.a23).
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        headers=_BROWSER_HEADERS,
        timeout=HTTP_TIMEOUT,
        follow_redirects=False,
        transport=transport,
    ) as client:
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            request = client.build_request("GET", current)
            response = await client.send(request, stream=True)
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise security.SourceError(
                            "bad_redirect",
                            f"redirect without Location (HTTP {response.status_code})",
                        )
                    current = validate_redirect_target(location, current)
                    continue
                chunks: List[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_BODY_BYTES:
                        raise security.SourceError(
                            "too_large",
                            f"response body exceeds {MAX_BODY_BYTES} bytes",
                        )
                    chunks.append(chunk)
                status = response.status_code
                body = b"".join(chunks)
                charset = response.charset_encoding or "utf-8"
            finally:
                await response.aclose()
            return status, body.decode(charset, errors="replace")
        raise security.SourceError(
            "too_many_redirects", f"more than {MAX_REDIRECTS} redirects for {url}"
        )


def _has_identity_markers(html_text: str) -> bool:
    return bool(_MARKER.search(html_text))


def _dump_dom(chromium: str, url: str) -> str:
    """Run chromium --headless --dump-dom via argv list only (no shell, no interpolation)."""
    argv = [
        chromium,
        "--headless=new",
        "--disable-gpu",
        "--dump-dom",
        "--no-sandbox",
        url,
    ]
    completed = subprocess.run(
        argv, capture_output=True, timeout=CHROMIUM_TIMEOUT_S, check=False
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")[:400]
        raise security.SourceError(
            "dump_dom_failed", f"chromium exited {completed.returncode}: {stderr}"
        )
    return completed.stdout.decode("utf-8", errors="replace")


async def fetch_page(
    url: str,
    cfg: Config,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Tuple[str, str]:
    """Fetch page HTML: plain HTTP first, headless chromium --dump-dom as fallback.

    Returns (html, method) where method is "http" or "dump-dom".
    Raises security.SourceError on validation/SSRF/redirect-guard failures and
    when no usable HTML could be obtained at all.
    """
    normalized = security.validate_source_url(url)
    target = normalized.url
    # SSRF baseline: the allowlisted host must still resolve to public addresses.
    await asyncio.to_thread(security.resolve_host_public, ALLOWED_HOST)

    status: Optional[int] = None
    body: Optional[str] = None
    problem: Optional[str] = None
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        try:
            status, body = await _http_get_chain(target, transport)
            break
        except security.SourceError as exc:
            if exc.code in _GUARD_CODES:
                raise
            problem = f"{exc.code}: {exc}"
        except httpx.HTTPError as exc:
            problem = f"{type(exc).__name__}: {exc}"
        status, body = None, None
        if attempt < HTTP_ATTEMPTS:  # flaky mobile DNS: one bounded retry
            await asyncio.sleep(0.75)

    if status is not None and 200 <= status < 300 and body is not None:
        if len(body) >= MIN_RENDERED_CHARS and _has_identity_markers(body):
            return body, "http"
        problem = problem or (
            f"HTTP 200 body looks non-rendered or blocked "
            f"({len(body)} chars, markers={'yes' if _has_identity_markers(body) else 'no'})"
        )
    elif status is not None:
        problem = problem or f"HTTP status {status}"

    chromium = find_chromium(cfg)
    if chromium is not None:
        try:
            dumped = await asyncio.to_thread(_dump_dom, chromium, target)
        except (OSError, subprocess.SubprocessError, security.SourceError) as exc:
            log.warning("dump-dom fallback failed: %s", exc)
            problem = f"{problem}; dump-dom failed: {exc}"
        else:
            if _has_identity_markers(dumped):
                return dumped, "dump-dom"
            problem = f"{problem}; dump-dom produced no identity markers ({len(dumped)} chars)"

    if status is not None and 200 <= status < 300 and body is not None:
        # Best effort: hand the HTTP body to the parser, which raises a clear
        # ValueError when no styles can be extracted.
        return body, "http"

    raise security.SourceError(
        "fetch_failed", f"cannot fetch {target}: {problem or 'unknown error'}"
    )


# ---------------------------------------------------------------------------
# Parsing (pure functions over HTML text)
# ---------------------------------------------------------------------------

def _clean_name(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    text = html_module.unescape(raw)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 2:
        return None
    return text[:120]


def _clean_page_title(raw: str) -> Optional[str]:
    text = re.sub(r"\s+", " ", html_module.unescape(raw)).strip()
    for sep in ("|", "\u2013", "\u2014", " - "):
        if sep in text:
            parts = [p.strip() for p in text.split(sep)]
            parts = [p for p in parts if p and p.casefold() != "myfonts"]
            if not parts:
                return None
            return parts[0][:120] or None
    if text.casefold() == "myfonts":
        return None
    return text[:120] or None


def _parse_attrs(tag: str) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for match in _ATTR.finditer(tag):
        name = match.group(1).lower()
        value = match.group(2) if match.group(2) is not None else match.group(3)
        attrs.setdefault(name, html_module.unescape(value))
    return attrs


def _iter_jsonld(html_text: str) -> Iterator[object]:
    for match in _JSONLD_BLOCK.finditer(html_text):
        try:
            yield json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            continue


def _jsonld_family(node: object) -> Optional[str]:
    if isinstance(node, dict):
        name = node.get("name")
        if isinstance(name, str):
            cleaned = _clean_name(name)
            if cleaned:
                return cleaned
        for value in node.values():
            found = _jsonld_family(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _jsonld_family(item)
            if found:
                return found
    return None


def _jsonld_brand(node: object) -> Optional[str]:
    if isinstance(node, dict):
        brand = node.get("brand")
        if isinstance(brand, str) and brand.strip():
            return brand.strip()[:120]
        if isinstance(brand, dict):
            name = brand.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()[:120]
        for value in node.values():
            found = _jsonld_brand(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _jsonld_brand(item)
            if found:
                return found
    return None


def _extract_family(html_text: str, source_url: str) -> Tuple[str, str]:
    match = _COLLECTION_TITLE.search(html_text)
    if match:
        cleaned = _clean_name(match.group(1) or match.group(2))
        if cleaned:
            return cleaned, "data-collection-title"
    for blob in _iter_jsonld(html_text):
        name = _jsonld_family(blob)
        if name:
            return name, "json-ld"
    match = _TITLE_TAG.search(html_text)
    if match:
        cleaned = _clean_page_title(match.group(1))
        if cleaned:
            return cleaned, "title-tag"
    match = _H1_TAG.search(html_text)
    if match:
        cleaned = _clean_name(re.sub(r"<[^>]+>", " ", match.group(1)))
        if cleaned:
            return cleaned, "h1"
    slug = urlsplit(source_url).path.rstrip("/").rsplit("/", 1)[-1]
    fallback = re.sub(r"[-_]+", " ", slug).strip().title() or "Unknown Family"
    return fallback, "url-slug"


def _extract_foundry(html_text: str) -> Tuple[Optional[str], Optional[str]]:
    match = _BRAND_JS.search(html_text)
    if match:
        cleaned = _clean_name(match.group(1) or match.group(2))
        if cleaned:
            return cleaned, "itemDataLayer.brand"
    for blob in _iter_jsonld(html_text):
        brand = _jsonld_brand(blob)
        if brand:
            return brand, "json-ld"
    match = _BRAND_ATTR.search(html_text)
    if match:
        cleaned = _clean_name(match.group(1) or match.group(2))
        if cleaned:
            return cleaned, "data-brand"
    return None, None


def infer_weight(name: Optional[str]) -> Optional[str]:
    """Map a style name token (Bold, ExtraLight, ...) to a canonical weight."""
    if not name:
        return None
    tokens = re.split(r"[\s_\-/]+", name.casefold())
    for token in tokens:
        for key in sorted(_WEIGHT_CANON, key=len, reverse=True):
            if token == key:
                return _WEIGHT_CANON[key]
    return None


def _find_nearby_name(html_text: str, start: int, end: int) -> Optional[str]:
    """Find a plausible style name near a data-md5hash occurrence."""
    anchored = _NAME_TEXT_AFTER_MD5HASH.search(html_text[start : end + NAME_WINDOW])
    if anchored:
        cleaned = _clean_name(anchored.group(1))
        if cleaned:
            return cleaned
    window = html_text[max(0, start - NAME_WINDOW) : end + NAME_WINDOW]
    for pattern in _NEARBY_NAME_RES:
        match = pattern.search(window)
        if match:
            cleaned = _clean_name(match.group(1) or match.group(2))
            if cleaned:
                return cleaned
    return None


def _style_page_url(html_text: str, pos: int, source_url: str) -> str:
    window = html_text[max(0, pos - NAME_WINDOW) : pos + NAME_WINDOW]
    match = _STYLE_LINK.search(window)
    if match:
        return f"https://{ALLOWED_HOST}{match.group(1) or match.group(2)}"
    return source_url


def _collect_candidates(html_text: str) -> List[Tuple[str, Optional[str], str, int]]:
    """Collect (md5, name_or_None, pattern, position) identity candidates."""
    candidates: List[Tuple[str, Optional[str], str, int]] = []

    # Pattern A: <font-render-image md5=... default="Style Name">
    for tag in _FONT_RENDER_TAG.finditer(html_text):
        attrs = _parse_attrs(tag.group(0))
        md5 = (attrs.get("md5") or attrs.get("data-md5hash") or "").strip().lower()
        if not _MD5_FULL.fullmatch(md5):
            continue
        name = _clean_name(
            attrs.get("default")
            or attrs.get("data-default")
            or attrs.get("alt")
            or attrs.get("title")
        )
        candidates.append((md5, name, "font-render-image", tag.start()))

    # Pattern B/C: data-md5hash attributes, named via nearby markers or bare.
    for match in _MD5HASH_ATTR.finditer(html_text):
        md5 = (match.group(1) or match.group(2)).lower()
        name = _find_nearby_name(html_text, match.start(), match.end())
        candidates.append((md5, name, "data-md5hash", match.start()))

    candidates.sort(key=lambda item: item[3])
    return candidates


def parse_collection(html_text: str, source_url: str) -> FontRequest:
    """Parse a MyFonts collection/family page into a FontRequest (pure function).

    Raises ValueError with a clear message when zero styles are found.
    """
    if not isinstance(html_text, str) or not html_text.strip():
        raise ValueError("parse_collection: empty page (no HTML to parse)")

    candidates = _collect_candidates(html_text)
    if not candidates:
        raise ValueError(
            "no font styles found: page exposes no md5 identity markers "
            "(font-render-image md5= / data-md5hash); page may be a bot "
            "challenge or non-rendered"
        )

    styles: List[StyleRef] = []
    by_md5: Dict[str, StyleRef] = {}
    pattern_counts: Dict[str, int] = {}
    for md5, name, pattern, pos in candidates:
        pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1
        existing = by_md5.get(md5)
        if existing is not None:
            if not existing.name and name:
                existing.name = name
                existing.weight = infer_weight(name)
                existing.metadata["name_source"] = pattern
            continue
        style = StyleRef(
            name=name or "",
            weight=infer_weight(name),
            identity=SourceIdentity.from_md5(md5),
            page_url=_style_page_url(html_text, pos, source_url),
            metadata={"name_source": pattern},
        )
        by_md5[md5] = style
        styles.append(style)

    synthesized = 0
    for index, style in enumerate(styles, 1):
        if not style.name:
            style.name = f"Style {index}"
            style.metadata["synthesized_name"] = True
            synthesized += 1

    family_name, family_source = _extract_family(html_text, source_url)
    foundry, foundry_source = _extract_foundry(html_text)

    patterns_summary = ", ".join(
        f"{name}x{count}" for name, count in sorted(pattern_counts.items())
    )
    notes = [
        f"styles={len(styles)} unique md5 identities ({patterns_summary})",
        f"family_name via {family_source}",
    ]
    if foundry:
        notes.append(f"foundry via {foundry_source}")
    if synthesized:
        notes.append(
            f"{synthesized} styles with synthesized names (bare data-md5hash fallback)"
        )

    return FontRequest(
        source_url=source_url,
        family_name=family_name,
        foundry=foundry,
        styles=styles,
        vietnamese=False,
        fetched_at=utc_now_iso(),
        fetch_method="http",
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def resolve(
    url: str,
    cfg: Config,
    vietnamese: bool = False,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> FontRequest:
    """Validate, fetch, and parse a MyFonts collection/family URL.

    Single entry point used by the orchestrator. Returns a FontRequest with
    source_url/vietnamese/fetched_at/fetch_method attached.
    """
    normalized = security.validate_source_url(url)
    html_text, method = await fetch_page(normalized.url, cfg, transport=transport)
    request = parse_collection(html_text, normalized.url)
    request.source_url = normalized.url
    request.vietnamese = bool(vietnamese)
    request.fetched_at = utc_now_iso()
    request.fetch_method = method
    request.notes.insert(0, f"fetch_method={method}")
    return request
