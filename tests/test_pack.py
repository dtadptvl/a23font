"""M6 ZIP packaging proofs (offline): layout mandate, manifest correctness,
secret redaction (M6.A3 no-secrets), safe component names (M6.A3 sanitized
names), the hollow-artifact guard, and cache-driven package collection.
"""
from __future__ import annotations

import hashlib
import json
import zipfile

from pipeline.cache import CacheStore
from pipeline.pack import (
    MIN_FONT_BYTES,
    StylePackage,
    build_collection_zip,
    collect_style_packages,
    safe_component,
)

FAMILY = "Pack Test"


def _fake_font(seed: int, size: int = 25000) -> bytes:
    """Deterministic pseudo-binary above the 20 KB hollow guard."""
    block = bytes((i * 7 + seed) % 256 for i in range(250))
    data = block * (size // len(block) + 1)
    return data[:size]


def _job(**overrides) -> dict:
    job = {
        "id": "J-packtest",
        "source_url": "https://www.myfonts.com/collections/pack-test?utm=x",
        "normalized_url": "https://www.myfonts.com/collections/pack-test",
        "options_json": json.dumps({"vietnamese": False, "schema": "1"}),
        "collection_name": "Pack Test Coll",
        "pipeline_version": "1",
    }
    job.update(overrides)
    return job


def _done_pkg(name: str, seed: int = 1, report_extra: dict | None = None) -> StylePackage:
    report = {
        "glyphs_frozen": 296,
        "glyphs_failed": 94,
        "cache_hit": False,
        "duration_s": 500.5,
        "validation": {"passed": True, "fonttools_ttf": {"passed": True}},
    }
    if report_extra:
        report.update(report_extra)
    return StylePackage(
        style_name=name,
        family_name=FAMILY,
        md5="ab" * 16,
        status="DONE",
        ttf=_fake_font(seed),
        otf=_fake_font(seed + 100),
        report=report,
    )


def _failed_pkg(name: str, code: str = "NETWORK", message: str = "live fetch failed") -> StylePackage:
    return StylePackage(
        style_name=name,
        family_name=FAMILY,
        md5="cd" * 16,
        status="FAILED",
        ttf=None,
        otf=None,
        report={
            "glyphs_frozen": 0,
            "glyphs_failed": 0,
            "cache_hit": False,
            "duration_s": 12.0,
            "error_code": code,
            "error_message": message,
        },
    )


# ---------------------------------------------------------------------------
# layout + manifest + hashes
# ---------------------------------------------------------------------------

def test_build_collection_zip_layout_counts_and_hashes(tmp_path):
    regular, bold = _done_pkg("Regular", seed=1), _done_pkg("Bold", seed=2)
    thin = _failed_pkg("Thin")
    out = tmp_path / "out" / "job.zip"
    summary = build_collection_zip(_job(), [regular, bold, thin], out)

    assert summary.path == out and out.is_file()
    data = out.read_bytes()
    assert summary.bytes == len(data)
    assert summary.sha256 == hashlib.sha256(data).hexdigest()

    zf = zipfile.ZipFile(out)
    names = zf.namelist()
    assert summary.entries == len(names) == 8  # 4 fonts + 3 reports + manifest
    fonts = sorted(n for n in names if n.startswith("fonts/"))
    reports = sorted(n for n in names if n.startswith("reports/"))
    assert fonts == [
        "fonts/Pack-Test-Bold.otf",
        "fonts/Pack-Test-Bold.ttf",
        "fonts/Pack-Test-Regular.otf",
        "fonts/Pack-Test-Regular.ttf",
    ]
    assert len(reports) == 3  # EVERY style (DONE and FAILED) gets a report
    # deterministic entry order: fonts sorted, reports sorted, manifest last
    assert names[:4] == fonts
    assert names[4:7] == reports
    assert names[7] == "manifest.json"
    # FAILED style has a report but no font entries
    assert "reports/Pack-Test-Thin.report.json" in names
    assert not any("Thin" in n for n in fonts)
    # font bytes round-trip unchanged
    assert zf.read("fonts/Pack-Test-Regular.ttf") == regular.ttf
    assert zf.read("fonts/Pack-Test-Bold.otf") == bold.otf

    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["service"] == "a23font"
    assert manifest["schema"] == 1
    assert manifest["job_id"] == "J-packtest"
    assert manifest["source_url"] == "https://www.myfonts.com/collections/pack-test"
    assert manifest["options"] == {"vietnamese": False}
    assert manifest["collection_name"] == "Pack-Test-Coll"
    assert manifest["pipeline_version"] == "1"
    assert manifest["generated_at"].endswith("+00:00")
    assert (manifest["styles_total"], manifest["styles_done"], manifest["styles_failed"]) == (3, 2, 1)
    assert manifest["validation_summary"] == {"done": 2, "failed": 1}
    assert manifest["notes"] == []

    by_name = {s["name"]: s for s in manifest["styles"]}
    reg = by_name["Regular"]
    assert reg["status"] == "DONE"
    assert reg["ttf_sha256"] == hashlib.sha256(regular.ttf).hexdigest()
    assert reg["otf_sha256"] == hashlib.sha256(regular.otf).hexdigest()
    assert reg["ttf_bytes"] == len(regular.ttf)
    assert reg["otf_bytes"] == len(regular.otf)
    assert reg["glyph_frozen"] == 296 and reg["glyph_failed"] == 94
    assert reg["cache_hit"] is False and reg["duration_s"] == 500.5
    assert reg["error_code"] is None and reg["error_message"] is None
    failed = by_name["Thin"]
    assert failed["status"] == "FAILED"
    assert failed["error_code"] == "NETWORK"
    assert failed["error_message"] == "live fetch failed"
    assert failed["ttf_sha256"] is None and failed["ttf_bytes"] is None

    failed_report = json.loads(zf.read("reports/Pack-Test-Thin.report.json"))
    assert failed_report["status"] == "FAILED"
    assert failed_report["report"]["error_code"] == "NETWORK"
    done_report = json.loads(zf.read("reports/Pack-Test-Regular.report.json"))
    assert done_report["report"]["validation"]["passed"] is True


def test_zip_bytes_are_deterministic(tmp_path):
    pkgs = [_done_pkg("Regular"), _failed_pkg("Thin")]
    one = build_collection_zip(_job(), pkgs, tmp_path / "a.zip")
    two = build_collection_zip(_job(), pkgs, tmp_path / "b.zip")
    # same inputs -> byte-identical archive (fixed timestamps + sorted order)
    assert one.sha256 == two.sha256


# ---------------------------------------------------------------------------
# M6.A3: no secrets in manifest or reports
# ---------------------------------------------------------------------------

def test_manifest_and_reports_redact_secret_keys(tmp_path):
    regular = _done_pkg(
        "Regular",
        report_extra={
            "api_token": "x",
            "nested": {"cookie": "y", "ok_field": 1},
            "headers": {"Authorization": "Bearer z"},
        },
    )
    summary = build_collection_zip(_job(), [regular, _failed_pkg("Thin")], tmp_path / "z.zip")
    zf = zipfile.ZipFile(tmp_path / "z.zip")
    text_entries = [n for n in zf.namelist() if n.endswith(".json")]
    assert len(text_entries) == 3  # manifest + one report per style
    all_text = "".join(zf.read(n).decode("utf-8") for n in text_entries)
    for banned in ("api_token", "cookie", "Authorization", "Bearer", '"x"', '"y"'):
        assert banned not in all_text
    manifest = json.loads(zf.read("manifest.json"))
    assert "redacted_keys=3" in manifest["notes"]
    # non-secret fields survive the redaction pass
    assert manifest["styles_done"] == 1
    done_report = json.loads(zf.read("reports/Pack-Test-Regular.report.json"))
    assert done_report["report"]["nested"] == {"ok_field": 1}


# ---------------------------------------------------------------------------
# M6.A3: sanitized component names
# ---------------------------------------------------------------------------

def test_safe_component_ascii_no_separators_and_dedupe():
    assert safe_component("Ábc/def:ghi") == "Abc-def-ghi"
    assert safe_component("") == "unnamed"
    assert safe_component("///") == "unnamed"
    assert safe_component(None) == "unnamed"
    backslash = safe_component("a\\b/c")
    assert "/" not in backslash and "\\" not in backslash
    assert len(safe_component("x" * 500)) <= 80
    seen: set = set()
    assert safe_component("Regular", seen) == "Regular"
    assert safe_component("Regular", seen) == "Regular-2"
    assert safe_component("Regular", seen) == "Regular-3"
    # case-insensitive dedupe keeps archives safe on any filesystem
    assert safe_component("regular", seen) == "regular-4"
    # collision suffixes keep the 80-char bound
    long_name = "L" * 80
    seen2: set = set()
    first = safe_component(long_name, seen2)
    second = safe_component(long_name, seen2)
    assert first != second and len(first) <= 80 and len(second) <= 80


def test_zip_entry_names_collide_with_suffixes(tmp_path):
    dup_a = _done_pkg("Regular", seed=1)
    dup_b = _done_pkg("Regular", seed=2)  # identical family+style name
    summary = build_collection_zip(_job(), [dup_a, dup_b], tmp_path / "c.zip")
    names = zipfile.ZipFile(tmp_path / "c.zip").namelist()
    fonts = sorted(n for n in names if n.startswith("fonts/"))
    assert fonts == [
        "fonts/Pack-Test-Regular-2.otf",
        "fonts/Pack-Test-Regular-2.ttf",
        "fonts/Pack-Test-Regular.otf",
        "fonts/Pack-Test-Regular.ttf",
    ]
    assert summary.entries == 4 + 2 + 1


# ---------------------------------------------------------------------------
# hollow-artifact guard
# ---------------------------------------------------------------------------

def test_hollow_done_package_is_skipped_and_counted(tmp_path):
    assert MIN_FONT_BYTES == 20000  # production guard, per mandate
    hollow = _done_pkg("Hollow")
    hollow.ttf = b"\x00" * 100  # 100-byte hollow artifact
    summary = build_collection_zip(_job(), [hollow], tmp_path / "h.zip")
    zf = zipfile.ZipFile(tmp_path / "h.zip")
    names = zf.namelist()
    assert not any(n.startswith("fonts/") for n in names)
    assert "reports/Pack-Test-Hollow.report.json" in names
    manifest = json.loads(zf.read("manifest.json"))
    assert "skipped_hollow=1" in manifest["notes"]
    assert manifest["styles_done"] == 1  # DB status stays DONE; zip stays honest
    assert manifest["styles"][0]["ttf_sha256"] is None
    assert summary.entries == 2  # report + manifest only


def test_done_package_missing_one_binary_counts_hollow(tmp_path):
    partial = _done_pkg("Partial")
    partial.otf = None  # lost artifact
    build_collection_zip(_job(), [partial], tmp_path / "p.zip")
    manifest = json.loads(zipfile.ZipFile(tmp_path / "p.zip").read("manifest.json"))
    assert "skipped_hollow=1" in manifest["notes"]


# ---------------------------------------------------------------------------
# collect_style_packages from cache artifacts
# ---------------------------------------------------------------------------

def test_collect_style_packages_from_cache(tmp_path):
    store = CacheStore(tmp_path / "cache", "1")
    options = {"vietnamese": False}
    md5_ok = "ab" * 16
    entry = store.begin(md5_ok, options, {"family": "Fam", "style": "Regular"})
    store.save_final(entry, b"t" * 25000, b"o" * 25000, {"passed": True})

    rows = [
        {
            "name": "Regular",
            "md5": md5_ok,
            "source_identity": "md5:" + md5_ok,
            "status": "DONE",
            "cache_hit": 1,
            "duration_ms": 1500,
            "report_json": json.dumps({"glyphs_frozen": 3, "glyphs_failed": 0}),
            "error_code": None,
            "error_message": None,
        },
        {
            "name": "Ghost",  # DONE row with no cache artifacts
            "md5": "ef" * 16,
            "source_identity": None,
            "status": "DONE",
            "cache_hit": 0,
            "duration_ms": 100,
            "report_json": None,
            "error_code": None,
            "error_message": None,
        },
        {
            "name": "Broken",
            "md5": "cd" * 16,
            "source_identity": None,
            "status": "FAILED",
            "cache_hit": 0,
            "duration_ms": 300,
            "report_json": None,
            "error_code": "NETWORK",
            "error_message": "live fetch failed",
        },
    ]
    pkgs = collect_style_packages(
        rows,
        cache_root=tmp_path / "cache",
        pipeline_version="1",
        options=options,
        default_family="Fallback",
    )
    assert [p.status for p in pkgs] == ["DONE", "FAILED", "FAILED"]
    assert pkgs[0].ttf == b"t" * 25000 and pkgs[0].otf == b"o" * 25000
    assert pkgs[0].family_name == "Fam"
    assert pkgs[0].report["validation"] == {"passed": True}
    assert pkgs[0].report["duration_s"] == 1.5
    assert pkgs[0].report["cache_hit"] is True
    # DONE without artifacts -> honest FAILED package
    assert pkgs[1].ttf is None
    assert pkgs[1].report["error_code"] == "ARTIFACT_MISSING"
    assert pkgs[1].family_name == "Fallback"
    # FAILED row carries the row error info
    assert pkgs[2].ttf is None
    assert pkgs[2].report["error_code"] == "NETWORK"
    assert pkgs[2].report["error_message"] == "live fetch failed"