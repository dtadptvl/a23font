"""Live verification for M4.A1: resolve one real MyFonts collection URL.

Run explicitly:  python -m pytest -m live -v
Excluded from the default offline suite via the `live` marker.
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import Config
from pipeline import source_myfonts

LIVE_COLLECTION_URL = "https://www.myfonts.com/collections/postamp-grotesk-font-fontfabric"


@pytest.mark.live
def test_live_resolve_collection(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    request = asyncio.run(source_myfonts.resolve(LIVE_COLLECTION_URL, cfg))
    assert request.family_name, "family name resolved"
    assert len(request.styles) >= 1, "at least one style resolved"
    first = request.styles[0]
    assert first.identity.kind == "md5", "style identity is a real md5"
    assert first.identity.confidence == "exact"
    print(
        f"\nLIVE family={request.family_name!r} foundry={request.foundry!r} "
        f"styles={len(request.styles)} method={request.fetch_method}"
    )
    print(f"LIVE first_style={first.name!r} md5={first.identity.value}")
    print(f"LIVE notes={request.notes}")
