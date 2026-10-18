"""Task 20a: crawl policy and SSRF guard security proofs.

The policy is pure logic: URL admission (scheme, userinfo, host, path) and
destination safety (resolved-address refusal). The crawler (20b) owns all
I/O through injectable seams, so these proofs run offline and deterministically.
"""

import pytest

from web_sync.crawl_policy import BlockedUrl, CrawlLimits, CrawlPolicy


def policy(**overrides) -> CrawlPolicy:
    defaults: dict = {
        "allowed_hosts": ["docs.example.com"],
        "allowed_prefixes": ("/docs/",),
    }
    defaults.update(overrides)
    return CrawlPolicy(**defaults)


class TestUrlAdmission:
    def test_accepts_allowlisted_https_url(self):
        url = "https://docs.example.com/docs/guide"
        assert policy().validate_url(url) == url

    def test_normalizes_case_port_and_fragment(self):
        assert policy().validate_url(
            "http://DOCS.example.com:80/docs/a#section"
        ) == "http://docs.example.com/docs/a"

    def test_keeps_nondefault_port_and_query(self):
        assert policy().validate_url(
            "https://docs.example.com:8443/docs/a?q=1"
        ) == "https://docs.example.com:8443/docs/a?q=1"

    def test_empty_path_becomes_root(self):
        assert policy(allowed_prefixes=[]).validate_url("https://docs.example.com") == (
            "https://docs.example.com/"
        )

    @pytest.mark.parametrize("url", [
        "ftp://docs.example.com/docs/a",
        "file:///etc/passwd",
        "javascript:alert(1)",
    ])
    def test_refuses_non_http_schemes(self, url):
        with pytest.raises(BlockedUrl) as caught:
            policy().validate_url(url)
        assert caught.value.reason == "scheme"

    def test_refuses_userinfo(self):
        with pytest.raises(BlockedUrl) as caught:
            policy().validate_url("https://u:p@docs.example.com/docs/a")
        assert caught.value.reason == "userinfo"

    def test_refuses_unlisted_host(self):
        with pytest.raises(BlockedUrl) as caught:
            policy().validate_url("https://evil.example.com/docs/a")
        assert caught.value.reason == "host"

    def test_refuses_suffix_spoofed_host(self):
        with pytest.raises(BlockedUrl) as caught:
            policy().validate_url("https://docs.example.com.evil.com/docs/a")
        assert caught.value.reason == "host"

    def test_refuses_path_outside_prefixes(self):
        with pytest.raises(BlockedUrl) as caught:
            policy().validate_url("https://docs.example.com/admin/secrets")
        assert caught.value.reason == "path"

    def test_no_prefixes_admits_every_path_on_host(self):
        url = "https://docs.example.com/anything"
        assert policy(allowed_prefixes=[]).validate_url(url) == url


class TestDestinationSafety:
    def test_accepts_global_addresses(self):
        policy().check_addresses(
            "docs.example.com", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]
        )

    @pytest.mark.parametrize("address", [
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.1.1",
        "240.0.0.1",
        "224.0.0.1",
        "0.0.0.0",
        "fc00::1",
        "::1",
        "fe80::1",
        "::ffff:10.0.0.1",
    ])
    def test_refuses_non_global_addresses(self, address):
        with pytest.raises(BlockedUrl) as caught:
            policy().check_addresses("docs.example.com", [address])
        assert caught.value.reason == "private_network"

    def test_refuses_unresolved_host(self):
        with pytest.raises(BlockedUrl) as caught:
            policy().check_addresses("docs.example.com", [])
        assert caught.value.reason == "unresolved"
        assert "unresolved" in str(caught.value)


class TestLimits:
    def test_defaults_match_spec_section_15(self):
        limits = CrawlLimits()
        assert limits.max_pages == 500
        assert limits.max_depth == 5
        assert limits.max_seconds == 1800.0
        assert limits.max_response_bytes == 10 * 1024 * 1024
