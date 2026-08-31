"""Security validation tests."""
import ipaddress
import socket

import pytest

from app.security import (
    SourceError,
    is_public_address,
    resolve_host_public,
    validate_source_url,
)


def test_accept_collection_url():
    result = validate_source_url("https://www.myfonts.com/collections/foo-bar")
    assert result.url == "https://www.myfonts.com/collections/foo-bar"
    assert result.path == "/collections/foo-bar"
    assert result.kind == "collection"


def test_accept_family_url():
    result = validate_source_url("https://www.myfonts.com/fonts/family-name")
    assert result.kind == "family"
    assert result.path == "/fonts/family-name"


def test_normalizes_apex_host_and_strips_query_fragment():
    result = validate_source_url("https://myfonts.com/collections/foo?utm_campaign=x#top")
    assert result.url == "https://www.myfonts.com/collections/foo"


def test_nested_segments_allowed():
    result = validate_source_url("https://www.myfonts.com/collections/foo/items/bar")
    assert result.path == "/collections/foo/items/bar"


def test_rejected_inputs_and_codes():
    cases = [
        ("http://www.myfonts.com/collections/foo", "bad_scheme"),
        ("https://fonts.google.com/collections/foo", "bad_host"),
        ("https://evilwww.myfonts.com/collections/foo", "bad_host"),
        ("https://www.myfonts.com/download/x", "bad_path"),
        ("https://www.myfonts.com/collections", "bad_path"),
        ("https://www.myfonts.com/collections/", "bad_path"),
        ("https://www.myfonts.com/collections/../secret", "bad_path"),
        ("https://x@www.myfonts.com/collections/foo", "bad_format"),
        ("https://www.myfonts.com:8443/collections/foo", "bad_format"),
        ("", "bad_format"),
        ("   ", "bad_format"),
    ]
    for raw, expected in cases:
        with pytest.raises(SourceError) as err:
            validate_source_url(raw)
        assert err.value.code == expected, raw


def test_too_long_url():
    raw = "https://www.myfonts.com/collections/" + "a" * 3000
    with pytest.raises(SourceError) as err:
        validate_source_url(raw)
    assert err.value.code == "too_long"


def test_is_public_address_rejects_non_public():
    for raw in ("127.0.0.1", "10.0.0.1", "169.254.1.1", "::1"):
        assert is_public_address(ipaddress.ip_address(raw)) is False
    assert is_public_address(ipaddress.ip_address("8.8.8.8")) is True


def test_resolve_host_public_rejects_private(monkeypatch):
    def fake_getaddrinfo(host, port, proto=0, **kwargs):
        return [(2, 1, 6, "", ("10.9.9.9", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SourceError) as err:
        resolve_host_public("www.myfonts.com")
    assert err.value.code == "unsafe_address"


def test_resolve_host_public_accepts_public(monkeypatch):
    def fake_getaddrinfo(host, port, proto=0, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert resolve_host_public("www.myfonts.com") == ["93.184.216.34"]


def test_resolve_host_dns_failure(monkeypatch):
    def fake_getaddrinfo(host, port, proto=0, **kwargs):
        raise OSError("no dns available")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SourceError) as err:
        resolve_host_public("www.myfonts.com")
    assert err.value.code == "dns_failure"
