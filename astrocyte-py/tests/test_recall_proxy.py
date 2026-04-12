"""Unit tests for recall/proxy.py — SSRF validation, URL expansion, hit parsing.

Tests the pure/sync functions without making real HTTP calls.
"""

from __future__ import annotations

import pytest

from astrocyte.recall.proxy import (
    PLACE_BANK,
    PLACE_QUERY,
    _deep_replace_placeholders,
    _expand_proxy_url,
    _httpx_url_and_query_params,
    _is_literal_ip_host,
    _parse_hits_payload,
    _proxy_recall_host_header_value,
    _row_to_hit,
    _unsafe_literal_ip,
    validate_proxy_recall_url,
)

# ---------------------------------------------------------------------------
# validate_proxy_recall_url — SSRF prevention
# ---------------------------------------------------------------------------


class TestValidateProxyRecallUrl:
    def test_valid_https(self):
        validate_proxy_recall_url("https://api.example.com/recall?q=test")

    def test_valid_http(self):
        validate_proxy_recall_url("http://api.example.com/recall")

    def test_empty_url_raises(self):
        with pytest.raises(ValueError, match="empty"):
            validate_proxy_recall_url("")

    def test_none_url_raises(self):
        with pytest.raises(ValueError, match="empty"):
            validate_proxy_recall_url(None)

    def test_ftp_scheme_rejected(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_proxy_recall_url("ftp://evil.com/data")

    def test_file_scheme_rejected(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_proxy_recall_url("file:///etc/passwd")

    def test_no_host_rejected(self):
        with pytest.raises(ValueError, match="no host"):
            validate_proxy_recall_url("http://")

    def test_localhost_rejected(self):
        with pytest.raises(ValueError, match="localhost"):
            validate_proxy_recall_url("http://localhost/api")

    def test_localhost_subdomain_rejected(self):
        with pytest.raises(ValueError, match="localhost"):
            validate_proxy_recall_url("http://evil.localhost/api")

    def test_loopback_ip_rejected(self):
        with pytest.raises(ValueError, match="loopback"):
            validate_proxy_recall_url("http://127.0.0.1/api")

    def test_private_ip_rejected(self):
        with pytest.raises(ValueError, match="private"):
            validate_proxy_recall_url("http://192.168.1.1/api")

    def test_private_10_network_rejected(self):
        with pytest.raises(ValueError, match="private"):
            validate_proxy_recall_url("http://10.0.0.1/api")

    def test_link_local_rejected(self):
        with pytest.raises(ValueError, match="link-local|private"):
            validate_proxy_recall_url("http://169.254.169.254/latest/meta-data")

    def test_cloud_metadata_ip_rejected(self):
        """AWS/GCP metadata endpoint must be blocked."""
        with pytest.raises(ValueError):
            validate_proxy_recall_url("http://169.254.169.254/latest/meta-data/")

    def test_ipv6_loopback_rejected(self):
        with pytest.raises(ValueError):
            validate_proxy_recall_url("http://[::1]/api")

    def test_whitespace_stripped(self):
        validate_proxy_recall_url("  https://api.example.com/recall  ")


# ---------------------------------------------------------------------------
# _unsafe_literal_ip
# ---------------------------------------------------------------------------


class TestUnsafeLiteralIp:
    def test_private_ip(self):
        assert _unsafe_literal_ip("192.168.1.1") is True

    def test_loopback(self):
        assert _unsafe_literal_ip("127.0.0.1") is True

    def test_public_ip(self):
        assert _unsafe_literal_ip("8.8.8.8") is False

    def test_not_ip(self):
        assert _unsafe_literal_ip("example.com") is False

    def test_link_local(self):
        assert _unsafe_literal_ip("169.254.169.254") is True


# ---------------------------------------------------------------------------
# _is_literal_ip_host
# ---------------------------------------------------------------------------


class TestIsLiteralIpHost:
    def test_ipv4(self):
        assert _is_literal_ip_host("1.2.3.4") is True

    def test_ipv6(self):
        assert _is_literal_ip_host("::1") is True

    def test_hostname(self):
        assert _is_literal_ip_host("example.com") is False


# ---------------------------------------------------------------------------
# _expand_proxy_url
# ---------------------------------------------------------------------------


class TestExpandProxyUrl:
    def test_replaces_placeholder(self):
        result = _expand_proxy_url("https://api.com/search?query={query}", "dark mode")
        assert "dark%20mode" in result
        assert "{query}" not in result

    def test_appends_query_param(self):
        result = _expand_proxy_url("https://api.com/search", "dark mode")
        assert "?q=dark%20mode" in result

    def test_appends_with_ampersand(self):
        result = _expand_proxy_url("https://api.com/search?limit=10", "test")
        assert "&q=test" in result


# ---------------------------------------------------------------------------
# _proxy_recall_host_header_value
# ---------------------------------------------------------------------------


class TestProxyRecallHostHeader:
    def test_default_https_port(self):
        assert _proxy_recall_host_header_value("example.com", 443, "https") == "example.com"

    def test_default_http_port(self):
        assert _proxy_recall_host_header_value("example.com", 80, "http") == "example.com"

    def test_non_default_port(self):
        assert _proxy_recall_host_header_value("example.com", 8080, "http") == "example.com:8080"

    def test_strips_trailing_dot(self):
        assert _proxy_recall_host_header_value("example.com.", 443, "https") == "example.com"


# ---------------------------------------------------------------------------
# _httpx_url_and_query_params
# ---------------------------------------------------------------------------


class TestHttpxUrlAndQueryParams:
    def test_url_without_query(self):
        url, params = _httpx_url_and_query_params("https://api.com/path")
        assert url == "https://api.com/path"
        assert params is None

    def test_url_with_query(self):
        url, params = _httpx_url_and_query_params("https://api.com/path?q=test&limit=10")
        assert "?" not in url
        assert params is not None
        assert ("q", "test") in params

    def test_empty_url(self):
        url, params = _httpx_url_and_query_params("")
        assert params is None


# ---------------------------------------------------------------------------
# _deep_replace_placeholders
# ---------------------------------------------------------------------------


class TestDeepReplacePlaceholders:
    def test_replaces_query_placeholder(self):
        result = _deep_replace_placeholders(PLACE_QUERY, "test query", "b1")
        assert result == "test query"

    def test_replaces_bank_placeholder(self):
        result = _deep_replace_placeholders(PLACE_BANK, "q", "my-bank")
        assert result == "my-bank"

    def test_replaces_in_dict(self):
        body = {"query": PLACE_QUERY, "bank": PLACE_BANK, "limit": 10}
        result = _deep_replace_placeholders(body, "search term", "b1")
        assert result == {"query": "search term", "bank": "b1", "limit": 10}

    def test_replaces_in_nested_list(self):
        body = {"items": [PLACE_QUERY, "static"]}
        result = _deep_replace_placeholders(body, "q", "b")
        assert result["items"] == ["q", "static"]

    def test_leaves_other_values(self):
        assert _deep_replace_placeholders(42, "q", "b") == 42
        assert _deep_replace_placeholders(None, "q", "b") is None


# ---------------------------------------------------------------------------
# _row_to_hit
# ---------------------------------------------------------------------------


class TestRowToHit:
    def test_valid_row(self):
        hit = _row_to_hit("src1", {"text": "hello", "score": 0.9})
        assert hit is not None
        assert hit.text == "hello"
        assert hit.score == 0.9
        assert hit.source == "proxy:src1"

    def test_missing_text_returns_none(self):
        assert _row_to_hit("src1", {"score": 0.9}) is None

    def test_empty_text_returns_none(self):
        assert _row_to_hit("src1", {"text": "", "score": 0.9}) is None
        assert _row_to_hit("src1", {"text": "  ", "score": 0.9}) is None

    def test_missing_score_defaults_to_half(self):
        hit = _row_to_hit("src1", {"text": "hello"})
        assert hit.score == 0.5

    def test_score_clamped(self):
        hit = _row_to_hit("src1", {"text": "a", "score": 5.0})
        assert hit.score == 1.0
        hit2 = _row_to_hit("src1", {"text": "a", "score": -1.0})
        assert hit2.score == 0.0

    def test_metadata_filtered(self):
        hit = _row_to_hit("src1", {
            "text": "a",
            "metadata": {"key": "val", "num": 42, "bad": [1, 2]},
        })
        assert hit.metadata == {"key": "val", "num": 42}
        assert "bad" not in hit.metadata  # list values filtered

    def test_tags_preserved(self):
        hit = _row_to_hit("src1", {"text": "a", "tags": ["t1", "t2"]})
        assert hit.tags == ["t1", "t2"]

    def test_memory_id_stringified(self):
        hit = _row_to_hit("src1", {"text": "a", "memory_id": 123})
        assert hit.memory_id == "123"

    def test_fact_type_preserved(self):
        hit = _row_to_hit("src1", {"text": "a", "fact_type": "world"})
        assert hit.fact_type == "world"


# ---------------------------------------------------------------------------
# _parse_hits_payload
# ---------------------------------------------------------------------------


class TestParseHitsPayload:
    def test_hits_key(self):
        data = {"hits": [{"text": "a"}, {"text": "b"}]}
        assert len(_parse_hits_payload(data)) == 2

    def test_results_key_fallback(self):
        data = {"results": [{"text": "a"}]}
        assert len(_parse_hits_payload(data)) == 1

    def test_no_hits_returns_empty(self):
        assert _parse_hits_payload({"other": "data"}) == []

    def test_non_dict_returns_empty(self):
        assert _parse_hits_payload([1, 2, 3]) == []

    def test_hits_not_list_returns_empty(self):
        assert _parse_hits_payload({"hits": "not a list"}) == []
