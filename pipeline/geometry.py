"""M3 geometry pass: unified public API.

Split into geometry_core (raster/contour/SDF/rasterize), geometry_fit
(simplification + cubic fitting) and geometry_check (candidate model +
confidence checks) to keep modules small; this module re-exports the full
contract surface so callers can `from pipeline import geometry`.
"""
from pipeline.geometry_core import (  # noqa: F401
    decode_raster,
    estimate_baseline,
    px_to_units,
    units_to_px,
    align_pair,
    merge_alpha,
    signed_distance,
    extract_contours,
    rasterize_glyph,
)
from pipeline.geometry_fit import (  # noqa: F401
    simplify_closed,
    fit_closed_cubic,
    sample_cubics,
    cubic_residual,
)
from pipeline.geometry_check import (  # noqa: F401
    CandidateGlyph,
    ConfidenceReport,
    confidence_checks,
)

__all__ = [
    "decode_raster",
    "estimate_baseline",
    "px_to_units",
    "units_to_px",
    "align_pair",
    "merge_alpha",
    "signed_distance",
    "extract_contours",
    "rasterize_glyph",
    "simplify_closed",
    "fit_closed_cubic",
    "sample_cubics",
    "cubic_residual",
    "CandidateGlyph",
    "ConfidenceReport",
    "confidence_checks",
]
