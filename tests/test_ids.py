"""Identifier helper tests."""
from app.ids import new_artifact_token, new_job_id, safe_slug


def test_safe_slug_basic():
    assert safe_slug("Hello World!") == "Hello-World"


def test_safe_slug_strips_diacritics():
    assert safe_slug("Caf\u00e9 Cr\u00e8me Br\u00fbl\u00e9e") == "Cafe-Creme-Brulee"


def test_safe_slug_keeps_case():
    assert safe_slug("FooBar 123") == "FooBar-123"


def test_safe_slug_empty_and_punctuation_only():
    assert safe_slug("") == "unnamed"
    assert safe_slug("!!! ???") == "unnamed"


def test_safe_slug_truncates():
    result = safe_slug("a" * 200, max_len=10)
    assert len(result) <= 10
    assert result == "aaaaaaaaaa"


def test_safe_slug_trailing_dash_after_truncation():
    assert safe_slug("abcde-fghij", max_len=6) == "abcde"


def test_job_ids_unique_with_prefix():
    ids = {new_job_id() for _ in range(64)}
    assert len(ids) == 64
    for value in ids:
        assert value.startswith("J-")
        assert len(value) > len("J-")


def test_artifact_token_length():
    token = new_artifact_token()
    assert len(token) >= 20
