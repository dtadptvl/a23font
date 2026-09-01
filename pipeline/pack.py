"""ZIP packaging for multi-style collection jobs (fast15 FINAL OUTPUT, M6).

Per-job ZIP layout (mandate):

    fonts/<Family>-<Style>.ttf / .otf     ONLY for DONE styles with real
                                          binaries (len > MIN_FONT_BYTES);
                                          hollow artifacts are skipped and
                                          counted (notes: skipped_hollow=N)
    reports/<Family>-<Style>.report.json  for EVERY style (DONE and FAILED:
                                          failure details, glyph counts,
                                          validation summary, duration,
                                          cache_hit)
    manifest.json                         root summary: identity, per-style
                                          hashes/sizes, validation summary,
                                          notes

Honesty / safety rules:
  * no secrets in the archive: a recursive redaction pass drops ANY key
    matching token/cookie/secret/password/authorization; the total count is
    recorded in manifest notes as "redacted_keys=N" (only when > 0);
  * source_url is the normalized public source URL only (no cookies/tokens);
  * deterministic output: fixed ZIP timestamps + sorted entry order
    (fonts sorted, reports sorted, manifest last), so identical inputs
    produce byte-identical archives (stable sha256).

Pure/testable by design: every function takes explicit inputs (job dict,
packages, paths) - no hidden globals, no DB access.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from app.ids import safe_slug
from pipeline.cache import CacheStore

__all__ = [
    "MIN_FONT_BYTES",
    "StylePackage",
    "ZipSummary",
    "build_collection_zip",
    "collect_style_packages",
    "safe_component",
]

# DONE styles must ship real binaries; anything <= this size is treated as a
# hollow artifact (e.g. a .notdef+space-only font from unusable observations)
# and excluded from fonts/ (counted in manifest notes instead).
MIN_FONT_BYTES = 20000

_REDACT_KEY = re.compile(r"token|cookie|secret|password|authorization", re.IGNORECASE)
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)  # fixed timestamp -> deterministic bytes
_TTF_NAME = "final.ttf"
_OTF_NAME = "final.otf"
_REPORT_NAME = "report.json"
_META_NAME = "meta.json"


# ---------------------------------------------------------------------------
# safe names
# ---------------------------------------------------------------------------

def safe_component(name: str, seen: Optional[Set[str]] = None) -> str:
    """Filesystem/URL-safe ASCII component for ZIP entry names.

    Reuses app.ids.safe_slug semantics (NFKD -> ASCII-only -> non-alnum runs
    become '-'): guaranteed non-empty ("unnamed"), <= 80 chars, no path
    separators, ASCII. When a `seen` set is passed, collisions are deduped
    with numeric suffixes: "name", "name-2", "name-3", ... (dedupe is
    case-insensitive so archives stay safe on case-preserving filesystems).
    """
    text = "" if name is None else str(name)
    base = safe_slug(text, max_len=80)
    if seen is None:
        return base
    candidate = base
    suffix_n = 2
    while candidate.lower() in seen:
        suffix = f"-{suffix_n}"
        head = safe_slug(base[: 80 - len(suffix)], max_len=80 - len(suffix))
        candidate = f"{head}{suffix}"
        suffix_n += 1
    seen.add(candidate.lower())
    return candidate


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------

@dataclass
class StylePackage:
    """One style's packageable outcome (binaries + report)."""

    style_name: str
    family_name: str
    md5: Optional[str]
    status: str  # "DONE" | "FAILED"
    ttf: Optional[bytes] = None
    otf: Optional[bytes] = None
    report: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ZipSummary:
    """Outcome of one collection ZIP build."""

    path: Path
    bytes: int
    entries: int
    sha256: str


# ---------------------------------------------------------------------------
# redaction (no secrets ever leave in an archive)
# ---------------------------------------------------------------------------

def _redact(obj: Any, counter: Dict[str, int]) -> Any:
    """Recursively drop dict keys that look like secrets; count drops."""
    if isinstance(obj, dict):
        cleaned: Dict[Any, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and _REDACT_KEY.search(key):
                counter["n"] += 1
                continue
            cleaned[key] = _redact(value, counter)
        return cleaned
    if isinstance(obj, list):
        return [_redact(item, counter) for item in obj]
    return obj


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _duration_s(row: Dict[str, Any]) -> Optional[float]:
    value = row.get("duration_ms")
    if value is None:
        return None
    try:
        return round(int(value) / 1000.0, 3)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# collection from cache artifacts (pure: explicit inputs only)
# ---------------------------------------------------------------------------

def collect_style_packages(
    style_rows: List[Dict[str, Any]],
    *,
    cache_root: Path,
    pipeline_version: str,
    options: Dict[str, Any],
    default_family: str = "",
) -> List[StylePackage]:
    """Build StylePackages from per-style cache artifacts + style rows.

    DONE rows load final.ttf/final.otf/report.json from their cache entry
    (candidates keyed by the raw md5 and by source_identity, so both cache
    keying schemes resolve). A DONE row without readable binaries becomes an
    honest FAILED package (ARTIFACT_MISSING). FAILED rows become FAILED
    packages carrying the row's error info. No hidden globals: cache root,
    pipeline version and build options are explicit parameters.
    """
    store = CacheStore(Path(cache_root), str(pipeline_version))
    build_options = dict(options or {})
    packages: List[StylePackage] = []
    for raw_row in style_rows:
        row = dict(raw_row)
        name = str(row.get("name") or "Style")
        md5 = row.get("md5")
        row_report = {}
        if row.get("report_json"):
            try:
                parsed = json.loads(row["report_json"])
                row_report = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                row_report = {}

        entry_dir: Optional[Path] = None
        if str(row.get("status")) == "DONE":
            for identity in (md5, row.get("source_identity")):
                if not identity:
                    continue
                candidate = store.dir_for(str(identity), build_options)
                if (candidate / _TTF_NAME).is_file() and (candidate / _OTF_NAME).is_file():
                    entry_dir = candidate
                    break

        if str(row.get("status")) == "DONE" and entry_dir is not None:
            meta = _load_json(entry_dir / _META_NAME)
            validation = _load_json(entry_dir / _REPORT_NAME)
            family = str(meta.get("family") or row_report.get("family") or default_family or "")
            try:
                frozen = int(meta.get("frozen_glyphs") or 0)
            except (TypeError, ValueError):
                frozen = 0
            report: Dict[str, Any] = {
                "family": family,
                "glyphs_total": row_report.get("glyphs_total", frozen),
                "glyphs_frozen": row_report.get("glyphs_frozen", frozen),
                "glyphs_failed": row_report.get("glyphs_failed", 0),
                "cache_hit": bool(row.get("cache_hit")),
                "duration_s": _duration_s(row),
                "validation": validation,
            }
            packages.append(
                StylePackage(
                    style_name=name,
                    family_name=family,
                    md5=md5,
                    status="DONE",
                    ttf=(entry_dir / _TTF_NAME).read_bytes(),
                    otf=(entry_dir / _OTF_NAME).read_bytes(),
                    report=report,
                )
            )
            continue

        if str(row.get("status")) == "DONE":
            code = "ARTIFACT_MISSING"
            message = "style DONE but no cached final.ttf/final.otf found"
        else:
            code = str(row.get("error_code") or "UNKNOWN")
            message = str(row.get("error_message") or "style failed")
        family = str(row_report.get("family") or default_family or "")
        packages.append(
            StylePackage(
                style_name=name,
                family_name=family,
                md5=md5,
                status="FAILED",
                ttf=None,
                otf=None,
                report={
                    "family": family,
                    "glyphs_total": row_report.get("glyphs_total", 0),
                    "glyphs_frozen": row_report.get("glyphs_frozen", 0),
                    "glyphs_failed": row_report.get("glyphs_failed", 0),
                    "cache_hit": bool(row.get("cache_hit")),
                    "duration_s": _duration_s(row),
                    "error_code": code,
                    "error_message": message,
                },
            )
        )
    return packages


# ---------------------------------------------------------------------------
# the ZIP build
# ---------------------------------------------------------------------------

def build_collection_zip(
    job: Dict[str, Any],
    styles: List[StylePackage],
    out_path: Path,
    *,
    min_font_bytes: Optional[int] = None,
) -> ZipSummary:
    """Package a collection job into outputs/<job_id>.zip (see module doc).

    min_font_bytes overrides the hollow-artifact guard for tests only; the
    production default is MIN_FONT_BYTES (20 KB).
    """
    threshold = MIN_FONT_BYTES if min_font_bytes is None else int(min_font_bytes)
    job = dict(job or {})
    job_id = str(job.get("id") or "job")
    try:
        options = json.loads(job.get("options_json") or "{}")
        if not isinstance(options, dict):
            options = {}
    except (TypeError, ValueError):
        options = {}
    vietnamese = bool(options.get("vietnamese"))
    source_url = job.get("normalized_url") or job.get("source_url")
    pipeline_version = str(
        job.get("pipeline_version") or options.get("schema") or "1"
    )

    counter = {"n": 0}
    notes: List[str] = []
    seen: Set[str] = set()
    font_entries: List[Tuple[str, bytes]] = []
    report_entries: List[Tuple[str, bytes]] = []
    manifest_styles: List[Dict[str, Any]] = []
    styles_done = 0
    styles_failed = 0
    skipped_hollow = 0

    for pkg in styles:
        base = safe_component(
            f"{pkg.family_name or 'Family'}-{pkg.style_name or 'Style'}", seen
        )
        report = _redact(dict(pkg.report or {}), counter)
        duration_s = report.get("duration_s")
        ttf_sha = otf_sha = None
        ttf_len = otf_len = None

        if pkg.status == "DONE":
            styles_done += 1
            real = (
                pkg.ttf is not None
                and pkg.otf is not None
                and len(pkg.ttf) > threshold
                and len(pkg.otf) > threshold
            )
            if real:
                font_entries.append((f"fonts/{base}.ttf", bytes(pkg.ttf)))
                font_entries.append((f"fonts/{base}.otf", bytes(pkg.otf)))
                ttf_sha = hashlib.sha256(bytes(pkg.ttf)).hexdigest()
                otf_sha = hashlib.sha256(bytes(pkg.otf)).hexdigest()
                ttf_len = len(pkg.ttf)
                otf_len = len(pkg.otf)
            else:
                skipped_hollow += 1
        else:
            styles_failed += 1

        manifest_styles.append(
            {
                "name": pkg.style_name,
                "family": pkg.family_name,
                "md5": pkg.md5,
                "status": pkg.status,
                "glyph_frozen": int(report.get("glyphs_frozen") or 0),
                "glyph_failed": int(report.get("glyphs_failed") or 0),
                "cache_hit": bool(report.get("cache_hit")),
                "duration_s": duration_s,
                "ttf_sha256": ttf_sha,
                "otf_sha256": otf_sha,
                "ttf_bytes": ttf_len,
                "otf_bytes": otf_len,
                "error_code": report.get("error_code"),
                "error_message": report.get("error_message"),
            }
        )

        report_doc = {
            "style": pkg.style_name,
            "family": pkg.family_name,
            "md5": pkg.md5,
            "status": pkg.status,
            "report": report,
        }
        report_bytes = json.dumps(
            report_doc, sort_keys=True, indent=2, ensure_ascii=False
        ).encode("utf-8")
        report_entries.append((f"reports/{base}.report.json", report_bytes))

    collection_raw = (
        job.get("collection_name")
        or (styles[0].family_name if styles else None)
        or job_id
    )
    manifest: Dict[str, Any] = {
        "service": "a23font",
        "schema": 1,
        "job_id": job_id,
        "source_url": source_url,
        "options": {"vietnamese": vietnamese},
        "collection_name": safe_component(collection_raw),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pipeline_version": pipeline_version,
        "styles_total": len(styles),
        "styles_done": styles_done,
        "styles_failed": styles_failed,
        "styles": manifest_styles,
        "validation_summary": {"done": styles_done, "failed": styles_failed},
        "notes": notes,
    }
    manifest = _redact(manifest, counter)
    if skipped_hollow > 0:
        manifest["notes"].append(f"skipped_hollow={skipped_hollow}")
    if counter["n"] > 0:
        manifest["notes"].append(f"redacted_keys={counter['n']}")
    manifest_bytes = json.dumps(
        manifest, sort_keys=True, indent=2, ensure_ascii=False
    ).encode("utf-8")

    font_entries.sort(key=lambda item: item[0])
    report_entries.sort(key=lambda item: item[0])

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    entries = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:

        def put(name: str, data: bytes) -> None:
            nonlocal entries
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
            entries += 1

        for name, data in font_entries:
            put(name, data)
        for name, data in report_entries:
            put(name, data)
        put("manifest.json", manifest_bytes)

    payload = out_path.read_bytes()
    return ZipSummary(
        path=out_path,
        bytes=len(payload),
        entries=entries,
        sha256=hashlib.sha256(payload).hexdigest(),
    )