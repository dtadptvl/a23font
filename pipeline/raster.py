"""Observation acquisition for the reconstruction pipeline (fast15.md, M5).

Source priority per requested observation (fast15 "source priority"):

    exact raster cache  ->  page bundle cell crop  ->  direct HTTP/CDN raster
    -> optional Chromium canvas-atlas hook (browser milestone)

Honesty rules:
  * the direct raster endpoint is only trusted at x_phase==0 / y_phase==0;
    phased requests return Observation(None, "missing", ...) unless an atlas
    hook is installed (the endpoint is not proven for phases yet).
  * acquire() NEVER raises; every failure becomes
    Observation(None, "failed", "<error>").
  * successful observations are written through to the exact raster cache
    (atomic compressed npz, alpha float32) so later runs/levels reuse them.

The page-bundle crop path is deliberately defensive: the real gmap bundle
schema is unknown, so several key spellings are tolerated for both the cell
rect (x/y/w/h, left/top/width/height, bbox list) and the page PNG payload.
"""
from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

import numpy as np
from PIL import Image

from pipeline.cache import atomic_write_bytes
from pipeline.discovery import glyph_raster_url
from pipeline.geometry_core import decode_raster

__all__ = [
    "OBSERVATION_SOURCES",
    "ObservationRequest",
    "Observation",
    "RasterProvider",
    "SyntheticRasterProvider",
]

LOGGER = logging.getLogger(__name__)

OBSERVATION_SOURCES = {"cache", "page_crop", "direct", "atlas", "missing", "failed"}

_LAYOUT_KEYS = ("layout", "glyphs", "cells", "gmap")
_IMAGE_KEYS = ("page_png", "png", "image", "page")


@dataclass(frozen=True)
class ObservationRequest:
    """One raster observation request for a glyph at a size + subpixel phase."""

    gid: str
    cp: int
    size_px: int
    x_phase: float = 0.0
    y_phase: float = 0.0


@dataclass
class Observation:
    """Acquisition result. alpha is float32 HxW in [0,1] or None when absent."""

    alpha: Optional[np.ndarray]
    source: str  # one of OBSERVATION_SOURCES
    note: str = ""


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(out):
        return None
    return out


class RasterProvider:
    """Async observation provider with exact raster cache write-through."""

    def __init__(
        self,
        cfg: Any,
        fetch_bytes: Optional[Callable[[str], Awaitable[Optional[bytes]]]] = None,
        fetch_page_bundle: Optional[Callable[[str], Awaitable[Any]]] = None,
        cache_obs_dir: Optional[Path] = None,
        atlas_acquire: Optional[Callable[[str, ObservationRequest], Awaitable[Optional[np.ndarray]]]] = None,
    ) -> None:
        self.cfg = cfg
        self.fetch_bytes = fetch_bytes
        self.fetch_page_bundle = fetch_page_bundle
        self.cache_obs_dir = Path(cache_obs_dir) if cache_obs_dir is not None else None
        # browser-milestone hook: async (md5, req) -> alpha | None
        self.atlas_acquire = atlas_acquire

    # -- exact raster cache ---------------------------------------------------

    @staticmethod
    def cache_key(md5: str, req: ObservationRequest) -> str:
        payload = f"{md5}|{req.gid}|{req.size_px}|{req.x_phase:.2f}|{req.y_phase:.2f}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def _cache_path(self, md5: str, req: ObservationRequest) -> Optional[Path]:
        if self.cache_obs_dir is None:
            return None
        return self.cache_obs_dir / (self.cache_key(md5, req) + ".npz")

    def _read_cache(self, md5: str, req: ObservationRequest) -> Optional[Observation]:
        path = self._cache_path(md5, req)
        if path is None or not path.is_file():
            return None
        try:
            with np.load(path) as data:
                alpha = np.asarray(data["alpha"], dtype=np.float32)
        except Exception as exc:  # noqa: BLE001 - corrupt cache must not poison runs
            LOGGER.warning("raster cache entry unreadable, dropping: %s (%s)", path, exc)
            try:
                path.unlink()
            except OSError:
                pass
            return None
        return Observation(alpha, "cache", f"exact raster cache hit {path.name}")

    def _write_cache(self, md5: str, req: ObservationRequest, alpha: np.ndarray) -> None:
        path = self._cache_path(md5, req)
        if path is None:
            return
        buf = io.BytesIO()
        np.savez_compressed(buf, alpha=np.ascontiguousarray(alpha, dtype=np.float32))
        atomic_write_bytes(path, buf.getvalue())

    # -- acquisition ------------------------------------------------------------

    async def acquire(self, md5: str, req: ObservationRequest) -> Observation:
        """Acquire one observation; never raises (failures -> "failed")."""
        try:
            return await self._acquire_inner(md5, req)
        except Exception as exc:  # noqa: BLE001 - contract: never raise
            return Observation(None, "failed", f"{type(exc).__name__}: {exc}")

    async def _acquire_inner(self, md5: str, req: ObservationRequest) -> Observation:
        hit = self._read_cache(md5, req)
        if hit is not None:
            return hit

        # page bundle cell crop (defensive; a bundle failure never blocks the
        # direct route, so errors here are logged and swallowed)
        if self.fetch_page_bundle is not None:
            try:
                obs = await self._acquire_page_crop(md5, req)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("page bundle crop failed for %s: %s", req.gid, exc)
                obs = None
            if obs is not None:
                self._write_cache(md5, req, obs.alpha)
                return obs

        phased = req.x_phase != 0.0 or req.y_phase != 0.0
        if not phased and self.fetch_bytes is not None:
            text = chr(req.cp) if req.cp > 0 else req.gid
            url = glyph_raster_url(md5, text, req.size_px)
            png = await self.fetch_bytes(url)
            if png is None:
                return Observation(None, "missing", "direct endpoint returned no payload")
            alpha = decode_raster(png)
            self._write_cache(md5, req, alpha)
            return Observation(alpha, "direct", url)

        # browser canvas-atlas hook (optional; browser milestone)
        if self.atlas_acquire is not None:
            try:
                alpha = await self.atlas_acquire(md5, req)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("atlas acquire failed for %s: %s", req.gid, exc)
                alpha = None
            if alpha is not None:
                alpha = np.asarray(alpha, dtype=np.float32)
                self._write_cache(md5, req, alpha)
                return Observation(alpha, "atlas", "browser canvas atlas cell")

        if phased:
            return Observation(None, "missing", "phase not supported by direct endpoint")
        return Observation(None, "missing", "no raster source configured")

    # -- page bundle crop ---------------------------------------------------------

    async def _acquire_page_crop(self, md5: str, req: ObservationRequest) -> Optional[Observation]:
        bundle = await self.fetch_page_bundle(md5)
        if not isinstance(bundle, dict):
            return None
        layout: Optional[Dict[str, Any]] = None
        for key in _LAYOUT_KEYS:
            cand = bundle.get(key)
            if isinstance(cand, dict):
                layout = cand
                break
        if not layout:
            return None
        meta = layout.get(req.gid)
        if not isinstance(meta, dict) and req.cp > 0:
            meta = layout.get(str(req.cp))
        if not isinstance(meta, dict):
            return None
        rect = self._extract_rect(meta)
        if rect is None:
            return None
        png: Optional[bytes] = None
        for key in _IMAGE_KEYS:
            cand = bundle.get(key)
            if isinstance(cand, (bytes, bytearray)) and len(cand) > 0:
                png = bytes(cand)
                break
        if png is None:
            return None
        x, y, w, h = rect
        img = Image.open(io.BytesIO(png))
        x0 = max(0, int(round(x)))
        y0 = max(0, int(round(y)))
        x1 = min(img.width, int(round(x + w)))
        y1 = min(img.height, int(round(y + h)))
        if x1 <= x0 or y1 <= y0:
            return None
        crop = img.crop((x0, y0, x1, y1))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        alpha = decode_raster(buf.getvalue())
        return Observation(
            alpha, "page_crop", f"cell x={x0} y={y0} w={x1 - x0} h={y1 - y0} from page bundle"
        )

    @staticmethod
    def _extract_rect(meta: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
        """Defensive cell-rect extraction: x/y/w/h, left/top/width/height, bbox."""
        x = _num(meta.get("x")) if _num(meta.get("x")) is not None else _num(meta.get("left"))
        y = _num(meta.get("y")) if _num(meta.get("y")) is not None else _num(meta.get("top"))
        w = _num(meta.get("w")) if _num(meta.get("w")) is not None else _num(meta.get("width"))
        h = _num(meta.get("h")) if _num(meta.get("h")) is not None else _num(meta.get("height"))
        if all(v is not None for v in (x, y, w, h)) and w > 0 and h > 0:
            return (x, y, w, h)
        bbox = meta.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            vals = [_num(v) for v in bbox]
            if all(v is not None for v in vals):
                x0, y0, a, b = vals
                if a > x0 and b > y0:  # x0,y0,x1,y1 form
                    return (x0, y0, a - x0, b - y0)
                if a > 0 and b > 0:  # x,y,w,h form
                    return (x0, y0, a, b)
        return None


# ---------------------------------------------------------------------------
# Synthetic test double (kept in-module so worker/pipeline tests share it)
# ---------------------------------------------------------------------------

def _shift_subpixel(alpha: np.ndarray, delta: float, axis: int) -> np.ndarray:
    """Translate alpha by `delta` pixels along `axis` (bilinear, zeros enter)."""
    n = alpha.shape[axis]
    idx = np.arange(n, dtype=np.float64) - float(delta)
    lo = np.floor(idx).astype(np.int64)
    frac = (idx - lo).astype(np.float32)
    shape = [1] * alpha.ndim
    shape[axis] = n
    frac = frac.reshape(shape)
    lo_ok = ((lo >= 0) & (lo < n)).reshape(shape)
    hi_ok = ((lo + 1 >= 0) & (lo + 1 < n)).reshape(shape)
    lo_v = np.take(alpha, np.clip(lo, 0, n - 1), axis=axis)
    hi_v = np.take(alpha, np.clip(lo + 1, 0, n - 1), axis=axis)
    out = np.where(lo_ok, lo_v * (1.0 - frac), 0.0) + np.where(hi_ok, hi_v * frac, 0.0)
    return out.astype(np.float32)


class SyntheticRasterProvider:
    """Deterministic offline provider: renders registered shapes into em cells.

    shapes maps gid -> draw fn(canvas) where the fn paints ink (alpha in
    [0,1]) into a float32 size x size zero canvas. Subpixel phases apply a
    deterministic fractional-pixel translation. Every acquire() call is
    counted under (gid, size_px, x_phase, y_phase) in `calls`.
    """

    def __init__(self, shapes: Dict[str, Callable[[np.ndarray], None]], supports_phases: bool = True) -> None:
        self.shapes = dict(shapes)
        self.supports_phases = bool(supports_phases)
        self.calls: Dict[Tuple[str, int, float, float], int] = {}

    def render(self, gid: str, size_px: int, x_phase: float = 0.0, y_phase: float = 0.0) -> np.ndarray:
        canvas = np.zeros((int(size_px), int(size_px)), dtype=np.float32)
        draw = self.shapes.get(gid)
        if draw is not None:
            draw(canvas)
        if x_phase:
            canvas = _shift_subpixel(canvas, float(x_phase), axis=1)
        if y_phase:
            canvas = _shift_subpixel(canvas, float(y_phase), axis=0)
        return canvas

    async def acquire(self, md5: str, req: ObservationRequest) -> Observation:
        key = (req.gid, int(req.size_px), float(req.x_phase), float(req.y_phase))
        self.calls[key] = self.calls.get(key, 0) + 1
        if req.gid not in self.shapes:
            return Observation(None, "missing", f"synthetic provider: no shape for {req.gid}")
        if (req.x_phase != 0.0 or req.y_phase != 0.0) and not self.supports_phases:
            return Observation(None, "missing", "synthetic provider: phases not supported")
        alpha = self.render(req.gid, req.size_px, req.x_phase, req.y_phase)
        if float(alpha.max()) <= 0.0:
            return Observation(None, "missing", f"synthetic provider: empty render for {req.gid}")
        return Observation(alpha, "direct", "synthetic render")