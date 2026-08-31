"""Identifier generation and slug helpers."""
from __future__ import annotations

import re
import secrets
import unicodedata

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def new_job_id() -> str:
    """Return a fresh job id like J-xxxxxxxxxxxxxxxx."""
    return "J-" + secrets.token_urlsafe(12)


def new_artifact_token() -> str:
    """Return an unguessable artifact download token."""
    return secrets.token_urlsafe(16)


def safe_slug(text: str, max_len: int = 60) -> str:
    """Build a filesystem/URL safe slug from text, preserving case."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _NON_ALNUM.sub("-", ascii_only).strip("-")
    if not slug:
        return "unnamed"
    slug = slug[:max_len].rstrip("-")
    if not slug:
        return "unnamed"
    return slug
