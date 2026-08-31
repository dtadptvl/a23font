"""Offline tests for pipeline.cache (M3.A3).

Covers identity/options segregation, schema+pipeline version gating,
validation gating, fontmodel-level hits, partial checkpoint resume,
atomic-write crash resilience, cumulative checkpoint idempotence,
corrupt-line tolerance, and invalidation semantics.
"""
from __future__ import annotations

import hashlib
import json

from pipeline.cache import (
    CACHE_SCHEMA_VERSION,
    CHECKPOINT_BATCH,
    CacheStore,
    atomic_write_bytes,
    atomic_write_json,
    dir_size,
    identity_key,
    options_key,
    total_size,
)

IDENTITY = "md5:" + "a" * 32
TTF = b"\x00\x01\x00\x00" + b"ttfdata" * 8
OTF = b"OTTO" + b"otfdata" * 8


def make_store(tmp_path, version="1") -> CacheStore:
    return CacheStore(tmp_path / "cache", pipeline_version=version)


def glyph_dicts(n, start=0):
    return [
        {"gid": f"g{start + i}", "cp": 65 + start + i, "advance": 500}
        for i in range(n)
    ]


def read_meta(entry_dir):
    return json.loads((entry_dir / "meta.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# options segregation
# ---------------------------------------------------------------------------

def test_options_segregation_and_dir_naming(tmp_path):
    store = make_store(tmp_path)
    opts_off = {"vietnamese": False}
    opts_on = {"vietnamese": True}
    dir_off = store.begin(IDENTITY, opts_off, {"family": "F", "style": "Regular"})
    dir_on = store.begin(IDENTITY, opts_on, {"family": "F", "style": "Bold"})
    assert dir_off != dir_on
    assert dir_off == store.root / f"{identity_key(IDENTITY)}-{options_key(opts_off)}"
    assert dir_on == store.root / f"{identity_key(IDENTITY)}-{options_key(opts_on)}"
    # canonical options hash: insertion order is irrelevant
    assert options_key({"a": 1, "b": 2}) == options_key({"b": 2, "a": 1})
    assert options_key(opts_off) != options_key(opts_on)

    store.checkpoint_glyphs(dir_off, glyph_dicts(2), "seed")
    assert store.lookup(IDENTITY, opts_off).status == "partial"
    # cross-options probe lands in the other dir, which has no checkpoint yet
    assert store.lookup(IDENTITY, opts_on).status == "miss"

    # simulate a hash collision / tamper: stored options disagree with probe
    meta = read_meta(dir_on)
    meta["options"] = opts_off
    atomic_write_json(dir_on / "meta.json", meta)
    assert store.lookup(IDENTITY, opts_on).status == "miss"


def test_key_shapes():
    assert CHECKPOINT_BATCH == 16
    assert CACHE_SCHEMA_VERSION == 1
    for key in (identity_key(IDENTITY), options_key({"vietnamese": True})):
        assert len(key) == 16
        assert all(c in "0123456789abcdef" for c in key)
    assert identity_key("md5:x") != identity_key("md5:y")


# ---------------------------------------------------------------------------
# version gating
# ---------------------------------------------------------------------------

def test_pipeline_version_gating(tmp_path):
    opts = {"vietnamese": False}
    store_v1 = make_store(tmp_path, "1")
    entry = store_v1.begin(IDENTITY, opts, {"family": "F", "style": "Regular"})
    store_v1.checkpoint_glyphs(entry, glyph_dicts(3), "batch")
    store_v1.save_fontmodel(entry, {"upem": 1000, "glyphs": {}})
    store_v1.save_final(entry, TTF, OTF, {"passed": True})

    assert CacheStore(tmp_path / "cache", "2").lookup(IDENTITY, opts).status == "miss"
    hit = CacheStore(tmp_path / "cache", "1").lookup(IDENTITY, opts)
    assert hit.status == "binary"
    assert hit.has_ttf and hit.has_otf


def test_schema_version_gating(tmp_path):
    opts = {}
    store = make_store(tmp_path)
    entry = store.begin(IDENTITY, opts, {})
    store.save_final(entry, TTF, OTF, {"passed": True})
    meta = read_meta(entry)
    meta["schema_version"] = 999
    atomic_write_json(entry / "meta.json", meta)
    assert store.lookup(IDENTITY, opts).status == "miss"


# ---------------------------------------------------------------------------
# validation gating
# ---------------------------------------------------------------------------

def test_validation_gating(tmp_path):
    store = make_store(tmp_path)
    opts = {}
    entry = store.begin(IDENTITY, opts, {"family": "F"})
    store.save_final(entry, TTF, OTF, {"passed": False, "errors": ["cmap"]})
    assert store.lookup(IDENTITY, opts).status != "binary"

    store.save_final(entry, TTF, OTF, {"passed": True})
    hit = store.lookup(IDENTITY, opts)
    assert hit.status == "binary"
    meta = read_meta(entry)
    assert meta["validation_passed"] is True
    assert meta["sha256_ttf"] == hashlib.sha256(TTF).hexdigest()
    assert meta["sha256_otf"] == hashlib.sha256(OTF).hexdigest()
    report = json.loads((entry / "report.json").read_text(encoding="utf-8"))
    assert report["passed"] is True


# ---------------------------------------------------------------------------
# fontmodel-level hit (no binaries yet)
# ---------------------------------------------------------------------------

def test_fontmodel_level_hit(tmp_path):
    store = make_store(tmp_path)
    opts = {"vietnamese": True}
    entry = store.begin(IDENTITY, opts, {"family": "F"})
    store.checkpoint_glyphs(entry, glyph_dicts(5), None)
    store.save_fontmodel(entry, {"upem": 1000})
    hit = store.lookup(IDENTITY, opts)
    assert hit.status == "fontmodel"
    assert hit.frozen_glyphs == 5
    assert not hit.has_ttf and not hit.has_otf


# ---------------------------------------------------------------------------
# partial checkpoint hit + resume load
# ---------------------------------------------------------------------------

def test_partial_checkpoint_hit(tmp_path):
    store = make_store(tmp_path)
    opts = {}
    entry = store.begin(IDENTITY, opts, {})
    assert store.lookup(IDENTITY, opts).status == "miss"

    store.checkpoint_glyphs(entry, glyph_dicts(3), "first batch")
    hit = store.lookup(IDENTITY, opts)
    assert hit.status == "partial"
    assert hit.frozen_glyphs == 3
    loaded = store.load_frozen_glyphs(entry)
    assert len(loaded) == 3
    assert loaded[0]["gid"] == "g0"
    assert hit.meta["last_checkpoint_note"] == "first batch"


# ---------------------------------------------------------------------------
# atomicity / crash resilience
# ---------------------------------------------------------------------------

def test_crash_leaves_meta_readable(tmp_path):
    store = make_store(tmp_path)
    entry = store.begin(IDENTITY, {}, {})
    store.checkpoint_glyphs(entry, glyph_dicts(4), "batch")
    before = (entry / "meta.json").read_bytes()

    # simulate a crash mid-write: a torn temp ledger file is left behind
    (entry / "meta.json.tmp.CRASH").write_bytes(b'{"status": "succ')

    hit = store.lookup(IDENTITY, {})
    assert hit.status == "partial"
    assert hit.frozen_glyphs == 4
    assert (entry / "meta.json").read_bytes() == before  # untouched


def test_atomic_write_leaves_no_tmp_residue(tmp_path):
    target = tmp_path / "nested" / "deep" / "file.json"
    atomic_write_json(target, {"a": 1})
    atomic_write_bytes(target.with_name("blob.bin"), b"xyz")
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    residue = [p.name for p in target.parent.iterdir() if ".tmp." in p.name]
    assert residue == []


def test_load_frozen_glyphs_skips_corrupt_lines(tmp_path):
    store = make_store(tmp_path)
    entry = store.begin(IDENTITY, {}, {})
    store.checkpoint_glyphs(entry, glyph_dicts(3), None)
    frozen = entry / "glyphs" / "frozen.jsonl"
    frozen.write_text(frozen.read_text(encoding="utf-8") + "{corrupt\n", encoding="utf-8")
    assert len(store.load_frozen_glyphs(entry)) == 3
    assert store.load_frozen_glyphs(tmp_path / "nowhere") == []


# ---------------------------------------------------------------------------
# checkpoint idempotence (cumulative rewrite)
# ---------------------------------------------------------------------------

def test_checkpoint_cumulative_rewrite(tmp_path):
    store = make_store(tmp_path)
    entry = store.begin(IDENTITY, {}, {})
    store.checkpoint_glyphs(entry, glyph_dicts(5), "5 frozen")
    store.checkpoint_glyphs(entry, glyph_dicts(20), "20 frozen")
    loaded = store.load_frozen_glyphs(entry)
    assert len(loaded) == 20
    assert store.lookup(IDENTITY, {}).frozen_glyphs == 20
    meta = read_meta(entry)
    assert meta["frozen_glyphs"] == 20
    assert meta["last_checkpoint_note"] == "20 frozen"
    lines = (entry / "glyphs" / "frozen.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 20  # rewrite, not append-on-append


# ---------------------------------------------------------------------------
# invalidation
# ---------------------------------------------------------------------------

def test_invalidate_keeps_artifacts(tmp_path):
    store = make_store(tmp_path)
    entry = store.begin(IDENTITY, {}, {})
    store.checkpoint_glyphs(entry, glyph_dicts(2), None)
    store.save_fontmodel(entry, {"upem": 1000})
    store.invalidate(entry, "validation failed: harfbuzz")

    meta = read_meta(entry)
    assert meta["status"] == "failed"
    assert meta["failure"] == "validation failed: harfbuzz"
    assert store.lookup(IDENTITY, {}).status == "miss"
    # artifacts survive invalidation (never deleted)
    assert (entry / "glyphs" / "frozen.jsonl").is_file()
    assert (entry / "fontmodel.json").is_file()


# ---------------------------------------------------------------------------
# size helpers
# ---------------------------------------------------------------------------

def test_size_helpers(tmp_path):
    store = make_store(tmp_path)
    entry = store.begin(IDENTITY, {}, {})
    store.save_final(entry, TTF, OTF, {"passed": True})
    assert dir_size(entry) >= len(TTF) + len(OTF)
    assert total_size(store.root) >= dir_size(entry)
    assert dir_size(tmp_path / "absent") == 0