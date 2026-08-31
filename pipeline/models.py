"""Source-agnostic internal models for the reconstruction pipeline.

Downstream stages (discovery, reconstruction, packaging) import these models
only. Source adapters (e.g. pipeline.source_myfonts) translate site-specific
HTML details into these types so no MyFonts/HTML detail leaks downstream.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

__all__ = ["SourceIdentity", "StyleRef", "FontRequest", "GlyphManifest", "utc_now_iso"]

_MD5_RE = re.compile(r"[0-9a-f]{32}")
_TRIPLE_SEP = "\x1f"


def utc_now_iso() -> str:
    """Current UTC timestamp as ISO-8601 with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_text(text: Optional[str]) -> str:
    """NFKD-normalize, casefold, and collapse whitespace runs to single spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text


@dataclass(frozen=True)
class SourceIdentity:
    """Stable identity of one font style as observed at the source.

    kind "md5" + confidence "exact": a real 32-hex md5 exposed by the source
    (drives the sig.monotype.com raster endpoint later).
    kind "fallback" + confidence "stable": deterministic sha256-based id over
    the normalized (family, style, foundry) triple, used when no md5 exists.
    """

    kind: Literal["md5", "fallback"]
    value: str
    confidence: Literal["exact", "stable", "low"]
    raw_ref: Optional[str] = None

    @property
    def stable_id(self) -> str:
        return f"{self.kind}:{self.value}"

    @classmethod
    def from_md5(cls, md5: str) -> "SourceIdentity":
        """Build an exact identity from a 32-hex md5 (case-insensitive)."""
        if not isinstance(md5, str):
            raise ValueError(f"md5 must be a string, got {type(md5).__name__}")
        normalized = md5.strip().lower()
        if not _MD5_RE.fullmatch(normalized):
            raise ValueError(f"not a 32-hex md5 identity: {md5!r}")
        return cls(kind="md5", value=normalized, confidence="exact", raw_ref=md5)

    @classmethod
    def fallback(cls, family: str, style: str, foundry: Optional[str] = None) -> "SourceIdentity":
        """Deterministic fallback identity: sha256 of the normalized triple, 32 hex chars."""
        triple = _TRIPLE_SEP.join(
            (_normalize_text(family), _normalize_text(style), _normalize_text(foundry))
        )
        digest = hashlib.sha256(triple.encode("utf-8")).hexdigest()[:32]
        return cls(kind="fallback", value=digest, confidence="stable", raw_ref=None)


@dataclass
class StyleRef:
    """One font style resolved from a source page."""

    name: str
    weight: Optional[str]
    identity: SourceIdentity
    page_url: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FontRequest:
    """Source-agnostic resolution result handed to the pipeline orchestrator."""

    source_url: str
    family_name: str
    foundry: Optional[str]
    styles: List[StyleRef]
    vietnamese: bool
    fetched_at: str
    fetch_method: str  # "http" | "dump-dom" | "cache"
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable dict for DB/report persistence."""
        return {
            "source_url": self.source_url,
            "family_name": self.family_name,
            "foundry": self.foundry,
            "vietnamese": self.vietnamese,
            "fetched_at": self.fetched_at,
            "fetch_method": self.fetch_method,
            "notes": list(self.notes),
            "styles": [
                {
                    "name": style.name,
                    "weight": style.weight,
                    "page_url": style.page_url,
                    "metadata": dict(style.metadata),
                    "identity": {
                        "kind": style.identity.kind,
                        "value": style.identity.value,
                        "confidence": style.identity.confidence,
                        "raw_ref": style.identity.raw_ref,
                        "stable_id": style.identity.stable_id,
                    },
                }
                for style in self.styles
            ],
        }


@dataclass
class GlyphManifest:
    """Glyph discovery result for one md5 identity (consumed by later stages).

    entries: per-glyph dicts recorded from gmap layout pages, each shaped
    {"gid": str, "cp": int, "meta": {...small scalar fields...}}.
    notes: human-readable stop/error details appended by discovery.
    glyphs: downstream frozen/observed glyph payloads. Kept with its default
    so existing keyword construction (md5/total_glyphs/unicode_coverage/pages/
    stop_reason) remains compatible.
    """

    md5: str
    total_glyphs: int
    unicode_coverage: List[int]
    pages: int
    stop_reason: str
    glyphs: List[Dict[str, Any]] = field(default_factory=list)
    entries: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)