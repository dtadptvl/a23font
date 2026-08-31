"""Persistent pipeline cache, checkpoint, and recovery (fast15.md, M3.A3).

Persistent artifacts only: source observations/measurements, frozen glyph
checkpoints, canonical FontModel, final TTF + OTF, validation report.
Ephemeral intermediates (decoded alpha, SDF, temporary contours, merge
buffers) never land here.

A cache hit must prove all five fast15 conditions:
  1. source identity       -> identity_key over SourceIdentity.stable_id / md5
  2. version compatibility -> CACHE_SCHEMA_VERSION + pipeline_version in meta
  3. successful build state -> meta.status == "success"
  4. validation state      -> meta.validation_passed + report.json
  5. relevant options      -> options_key dir suffix + stored options equality

Every ledger/artifact write goes through a temp file + os.replace, so a crash
mid-write never corrupts the previous good state (no torn meta.json). No
fsync/checkpoint after every glyph: checkpoints are batched (CHECKPOINT_BATCH)
and each checkpoint rewrites the cumulative glyphs/frozen.jsonl in one pass.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from pipeline.models import utc_now_iso

__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CHECKPOINT_BATCH",
    "CacheLookup",
    "CacheStore",
    "atomic_write_bytes",
    "atomic_write_json",
    "dir_size",
    "identity_key",
    "options_key",
    "total_size",
]

LOGGER = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = 1
# fast15: checkpoint every 16 frozen glyphs (or completed atlas page/shutdown).
CHECKPOINT_BATCH = 16

_META_NAME = "meta.json"
_FONTMODEL_NAME = "fontmodel.json"
_REPORT_NAME = "report.json"
_TTF_NAME = "final.ttf"
_OTF_NAME = "final.otf"
_FROZEN_REL = Path("glyphs") / "frozen.jsonl"


def options_key(options: dict) -> str:
    """Canonical sha256 over the options dict (sorted keys, compact), 16 hex."""
    payload = json.dumps(options or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def identity_key(identity_str: str) -> str:
    """sha256 of the identity string (stable_id or raw md5 identity), 16 hex."""
    return hashlib.sha256(str(identity_str).encode("utf-8")).hexdigest()[:16]


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes via <path>.tmp.<rand> + os.replace; parent dirs auto-created.

    The replace is atomic within the directory, so readers observe either the
    previous complete file or the new complete file, never a torn write. A
    failed write removes its temp file and leaves the target untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp.{secrets.token_hex(4)}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(bytes(data))
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj: Any) -> None:
    """Deterministic JSON (sorted keys, compact) written atomically as UTF-8."""
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    atomic_write_bytes(path, payload.encode("utf-8"))


def dir_size(path: Path) -> int:
    """Recursive byte count of all files under path (0 when absent)."""
    path = Path(path)
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def total_size(root: Path) -> int:
    """Recursive byte count across the whole cache root."""
    return dir_size(root)


def _read_meta(entry_dir: Path) -> Optional[Dict[str, Any]]:
    """Best-effort meta.json read; None when absent/unreadable/not a dict."""
    try:
        with open(Path(entry_dir) / _META_NAME, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


@dataclass(frozen=True)
class CacheLookup:
    """Outcome of one cache probe."""

    dir: Path
    status: str  # "binary" | "fontmodel" | "partial" | "miss"
    meta: Optional[Dict[str, Any]]
    has_ttf: bool
    has_otf: bool
    frozen_glyphs: int


class CacheStore:
    """Directory-backed cache keyed by (source identity, build options)."""

    def __init__(self, root: Path, pipeline_version: str) -> None:
        self.root = Path(root)
        self.pipeline_version = str(pipeline_version)

    # -- addressing ---------------------------------------------------------

    def dir_for(self, identity_str: str, options: dict) -> Path:
        """Entry directory: root / <identity_key>-<options_key>."""
        return self.root / f"{identity_key(identity_str)}-{options_key(options)}"

    # -- lifecycle ----------------------------------------------------------

    def begin(self, identity_str: str, options: dict, meta: dict) -> Path:
        """Create the entry dir and write the initial in-progress ledger.

        Caller meta may carry family/style and other context; the locked
        ledger fields below always take precedence.
        """
        entry_dir = self.dir_for(identity_str, options)
        entry_dir.mkdir(parents=True, exist_ok=True)
        now = utc_now_iso()
        merged: Dict[str, Any] = dict(meta or {})
        merged.setdefault("family", None)
        merged.setdefault("style", None)
        merged.update(
            {
                "schema_version": CACHE_SCHEMA_VERSION,
                "pipeline_version": self.pipeline_version,
                "identity": identity_str,
                "options": options,
                "status": "in_progress",
                "created_at": now,
                "updated_at": now,
                "frozen_glyphs": 0,
                "validation_passed": False,
            }
        )
        atomic_write_json(entry_dir / _META_NAME, merged)
        return entry_dir

    def lookup(self, identity_str: str, options: dict) -> CacheLookup:
        """Probe the entry for identity+options; a hit must prove identity,
        schema/pipeline version compatibility, options equality, and the
        build/validation state recorded in the ledger.
        """
        entry_dir = self.dir_for(identity_str, options)
        if not entry_dir.is_dir():
            return CacheLookup(entry_dir, "miss", None, False, False, 0)
        meta = _read_meta(entry_dir)
        if meta is None:
            return CacheLookup(entry_dir, "miss", None, False, False, 0)
        if meta.get("schema_version") != CACHE_SCHEMA_VERSION:
            return CacheLookup(entry_dir, "miss", None, False, False, 0)
        if str(meta.get("pipeline_version")) != self.pipeline_version:
            return CacheLookup(entry_dir, "miss", None, False, False, 0)
        if meta.get("options") != options:
            return CacheLookup(entry_dir, "miss", None, False, False, 0)

        has_ttf = (entry_dir / _TTF_NAME).is_file()
        has_otf = (entry_dir / _OTF_NAME).is_file()
        try:
            frozen = int(meta.get("frozen_glyphs") or 0)
        except (TypeError, ValueError):
            frozen = 0

        status = meta.get("status")
        if (
            status == "success"
            and meta.get("validation_passed") is True
            and has_ttf
            and has_otf
        ):
            return CacheLookup(entry_dir, "binary", meta, True, True, frozen)
        if (
            status == "success"
            and (entry_dir / _FONTMODEL_NAME).is_file()
            and not (has_ttf and has_otf)
        ):
            return CacheLookup(entry_dir, "fontmodel", meta, has_ttf, has_otf, frozen)
        if (
            status == "in_progress"
            and (entry_dir / _FROZEN_REL).is_file()
            and frozen > 0
        ):
            return CacheLookup(entry_dir, "partial", meta, has_ttf, has_otf, frozen)
        return CacheLookup(entry_dir, "miss", meta, has_ttf, has_otf, frozen)

    # -- checkpoints --------------------------------------------------------

    def checkpoint_glyphs(
        self, dir: Path, glyph_dicts: List[dict], batch_note: Optional[str] = None
    ) -> Path:
        """Atomically rewrite glyphs/frozen.jsonl with ALL frozen glyphs so far.

        The caller passes the cumulative list; one atomic rewrite per
        checkpoint batch, never one write/fsync per glyph (fast15). The meta
        ledger is then updated with the frozen count in the same batched step.
        """
        entry_dir = Path(dir)
        lines = [
            json.dumps(glyph, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            for glyph in glyph_dicts
        ]
        payload = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
        frozen_path = entry_dir / _FROZEN_REL
        atomic_write_bytes(frozen_path, payload)
        meta = _read_meta(entry_dir) or {}
        meta["frozen_glyphs"] = len(glyph_dicts)
        meta["updated_at"] = utc_now_iso()
        if batch_note is not None:
            meta["last_checkpoint_note"] = batch_note
        atomic_write_json(entry_dir / _META_NAME, meta)
        return frozen_path

    def load_frozen_glyphs(self, dir: Path) -> List[dict]:
        """Load checkpointed glyph dicts; absent file -> [], corrupt lines
        are skipped and their count is recorded in the module log."""
        path = Path(dir) / _FROZEN_REL
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []
        glyphs: List[dict] = []
        skipped = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if isinstance(obj, dict):
                glyphs.append(obj)
            else:
                skipped += 1
        if skipped:
            LOGGER.warning(
                "load_frozen_glyphs: skipped %d corrupt line(s) in %s", skipped, path
            )
        return glyphs

    # -- finalization -------------------------------------------------------

    def save_fontmodel(self, dir: Path, model_dict: dict) -> None:
        """Persist the canonical FontModel (persistent fast15 artifact).

        A completed canonical FontModel is itself a recovery point even before
        binaries exist, so the ledger advances to success; an entry already
        marked failed stays failed (invalidation is sticky).
        """
        entry_dir = Path(dir)
        atomic_write_json(entry_dir / _FONTMODEL_NAME, model_dict)
        meta = _read_meta(entry_dir) or {}
        if meta.get("status") != "failed":
            meta["status"] = "success"
        meta["updated_at"] = utc_now_iso()
        atomic_write_json(entry_dir / _META_NAME, meta)

    def save_final(self, dir: Path, ttf_bytes: bytes, otf_bytes: bytes, report: dict) -> None:
        """Persist final TTF+OTF + validation report and close the entry."""
        entry_dir = Path(dir)
        atomic_write_bytes(entry_dir / _TTF_NAME, bytes(ttf_bytes))
        atomic_write_bytes(entry_dir / _OTF_NAME, bytes(otf_bytes))
        atomic_write_json(entry_dir / _REPORT_NAME, report)
        meta = _read_meta(entry_dir) or {}
        meta["status"] = "success"
        meta["validation_passed"] = bool(report.get("passed"))
        meta["sha256_ttf"] = hashlib.sha256(bytes(ttf_bytes)).hexdigest()
        meta["sha256_otf"] = hashlib.sha256(bytes(otf_bytes)).hexdigest()
        meta["updated_at"] = utc_now_iso()
        atomic_write_json(entry_dir / _META_NAME, meta)

    def invalidate(self, dir: Path, reason: str) -> None:
        """Mark the entry failed with a reason; artifacts are never deleted."""
        entry_dir = Path(dir)
        meta = _read_meta(entry_dir) or {
            "schema_version": CACHE_SCHEMA_VERSION,
            "pipeline_version": self.pipeline_version,
            "created_at": utc_now_iso(),
        }
        meta["status"] = "failed"
        meta["failure"] = reason
        meta["updated_at"] = utc_now_iso()
        atomic_write_json(entry_dir / _META_NAME, meta)