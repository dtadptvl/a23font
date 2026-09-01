"""Metrics producers for the reconstruction pipeline (fast15.md, M5).

Two producers:
  * estimate_from_manifest: the offline HEURISTIC estimator used in this
    milestone. Advances come from gmap layout meta (advance/adv keys) when
    present, otherwise from observed ink width * 1.18, otherwise the 500-unit
    fallback. Ascender/descender come from observed ink extents mapped to
    font units (assuming the canonical 0.80 baseline fraction for the
    observation cells), clamped to sane ranges; canonical 800/-200 defaults
    apply when no ink observations exist.
  * BrowserMetricsProvider: the persistent-Chromium measureText producer
    (fast15 "METRICS PRODUCER"). The browser milestone is PENDING, so the
    interface raises NotImplementedError and the orchestrator must tolerate
    its absence and use the heuristic. Never faked.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ["MetricsEstimate", "estimate_from_manifest", "BrowserMetricsProvider"]

UP_DEFAULT = 1000
BASE_SIZE_DEFAULT = 1024

_ASC_DEFAULT = 800.0
_DESC_DEFAULT = -200.0
_ASC_MIN, _ASC_MAX = 600.0, 1000.0
_DESC_MIN, _DESC_MAX = -400.0, -50.0
_ADVANCE_FALLBACK = 500.0
_INK_ADVANCE_FACTOR = 1.18
# Canonical cell baseline assumption for mapping ink rows to font units.
_BASELINE_FRACTION = 0.80


@dataclass
class MetricsEstimate:
    """Global + per-glyph metrics estimate in font units (UPEM as given)."""

    advance_units: Dict[str, float] = field(default_factory=dict)
    ascender_units: float = _ASC_DEFAULT
    descender_units: float = _DESC_DEFAULT
    method: str = "heuristic"
    notes: List[str] = field(default_factory=list)


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _summary_field(summary: Any, key: str) -> Optional[float]:
    if not isinstance(summary, dict):
        return None
    return _num(summary.get(key))


def estimate_from_manifest(
    manifest: Any,
    observations_summary: Optional[Dict[str, Any]] = None,
    upem: int = UP_DEFAULT,
    base_size_px: int = BASE_SIZE_DEFAULT,
) -> MetricsEstimate:
    """Heuristic metrics estimate from manifest layout meta + ink summaries.

    observations_summary maps gid -> dict with optional numeric keys
    size_px, ink_left, ink_right, ink_top, ink_bottom (pixels in the
    observation cell). It may be empty/None: defaults then apply.
    """
    notes: List[str] = []
    summaries = dict(observations_summary or {})
    advance: Dict[str, float] = {}
    n_from_meta = 0
    n_from_ink = 0
    n_fallback = 0

    entries = getattr(manifest, "entries", None)
    if entries is None and isinstance(manifest, dict):
        entries = manifest.get("entries", [])
    entries = list(entries or [])

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        gid = str(entry.get("gid", ""))
        if not gid:
            continue
        meta = entry.get("meta") or {}
        raw = meta.get("advance") if meta.get("advance") is not None else meta.get("adv")
        adv = _num(raw)
        if adv is not None and adv > 0:
            advance[gid] = adv
            n_from_meta += 1
            continue
        ink_left = _summary_field(summaries.get(gid), "ink_left")
        ink_right = _summary_field(summaries.get(gid), "ink_right")
        size_px = _summary_field(summaries.get(gid), "size_px") or float(base_size_px)
        if ink_left is not None and ink_right is not None and ink_right > ink_left and size_px > 0:
            ink_width_units = (ink_right - ink_left) * float(upem) / float(size_px)
            advance[gid] = round(ink_width_units * _INK_ADVANCE_FACTOR, 1)
            n_from_ink += 1
        else:
            advance[gid] = _ADVANCE_FALLBACK
            n_fallback += 1

    # ascender / descender from observed ink extents
    asc_raw: Optional[float] = None
    desc_raw: Optional[float] = None
    for gid, summary in summaries.items():
        size_px = _summary_field(summary, "size_px") or float(base_size_px)
        if size_px <= 0:
            continue
        baseline_px = _BASELINE_FRACTION * size_px
        scale = float(upem) / float(size_px)
        top = _summary_field(summary, "ink_top")
        bottom = _summary_field(summary, "ink_bottom")
        if top is not None:
            asc_units = (baseline_px - top) * scale
            asc_raw = asc_units if asc_raw is None else max(asc_raw, asc_units)
        if bottom is not None:
            desc_units = (baseline_px - bottom) * scale
            desc_raw = desc_units if desc_raw is None else min(desc_raw, desc_units)

    if asc_raw is None:
        ascender = _ASC_DEFAULT
        notes.append(
            f"ascender: no ink observations; canonical default {_ASC_DEFAULT:.0f}"
        )
    else:
        ascender = min(max(asc_raw, _ASC_MIN), _ASC_MAX)
        notes.append(
            f"ascender: observed {asc_raw:.1f} clamped to [{_ASC_MIN:.0f},{_ASC_MAX:.0f}] -> {ascender:.1f}"
        )
    if desc_raw is None:
        descender = _DESC_DEFAULT
        notes.append(
            f"descender: no ink observations; canonical default {_DESC_DEFAULT:.0f}"
        )
    else:
        descender = min(max(desc_raw, _DESC_MIN), _DESC_MAX)
        notes.append(
            f"descender: observed {desc_raw:.1f} clamped to [{_DESC_MIN:.0f},{_DESC_MAX:.0f}] -> {descender:.1f}"
        )

    notes.append(
        f"advances: {n_from_meta} from layout meta, {n_from_ink} from ink width*{_INK_ADVANCE_FACTOR}, "
        f"{n_fallback} fallback {_ADVANCE_FALLBACK:.0f}"
    )
    return MetricsEstimate(
        advance_units=advance,
        ascender_units=ascender,
        descender_units=descender,
        method="heuristic",
        notes=notes,
    )


class BrowserMetricsProvider:
    """Persistent-Chromium measureText metrics producer (fast15.md).

    Milestone PENDING: browser measurement (widths, actual bbox,
    ascent/descent, font bbox at 512/1024/2048 with multi-size regression to
    UPEM=1000) is not part of this build. The orchestrator tolerates the
    absence and falls back to estimate_from_manifest.
    """

    def measure_batch(self, md5: str, texts: List[str], sizes=(512, 1024, 2048)) -> Dict[str, Any]:
        raise NotImplementedError("browser metrics milestone pending")