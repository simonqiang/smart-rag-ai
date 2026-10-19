"""Task 20a/20b: crawl policy, SSRF guard, and crawler engine security proofs.

The policy is pure logic: URL admission (scheme, userinfo, host, path) and
destination safety (resolved-address refusal). The crawler owns all I/O
through injectable seams, so every proof runs offline and deterministically.
"""

import pytest

from web_sync.crawl_policy import BlockedUrl, CrawlLimits, CrawlPolicy
from web_sync.crawler import CrawlDefinition, CrawlFailed, FetchResponse, crawl


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


# --- Task 20b: crawler engine -------------------------------------------


class FakeSite:
    """In-memory site: canned DNS, canned responses, recorded fetch calls.

    An unlisted URL raises KeyError, so an unexpected fetch fails the test.
    """

    def __init__(self, addresses: dict[str, list[str]] | None = None) -> None:
        self.addresses = (
            {"docs.example.com": ["93.184.216.34"]} if addresses is None else addresses
        )
        self.responses: dict[str, FetchResponse | Exception] = {}
        self.fetched: list[str] = []

    def resolve(self, host: str) -> list[str]:
        return list(self.addresses.get(host, []))

    def fetch(self, url: str) -> FetchResponse:
        self.fetched.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def html_page(text: str, *hrefs: str) -> FetchResponse:
    anchors = "".join(f'<a class="l" href="{href}">x</a>' for href in hrefs)
    body = f"<body><p>{text}</p>{anchors}</body>".encode()
    return FetchResponse(200, "text/html", body)


def start(defn_url: str = "https://docs.example.com/docs/", **policy_args):
    return CrawlDefinition(defn_url, policy(**policy_args))


class TestCrawler:
    def test_bfs_crawls_linked_pages_and_parses_blocks(self):
        site = FakeSite()
        site.responses = {
            "https://docs.example.com/docs/": html_page("start", "/docs/a"),
            "https://docs.example.com/docs/a": html_page("page a", "/docs/"),
        }
        result = crawl(start(), resolve=site.resolve, fetch=site.fetch)
        assert [page.url for page in result.pages] == [
            "https://docs.example.com/docs/",
            "https://docs.example.com/docs/a",
        ]
        assert [block.text for block in result.pages[0].blocks] == ["start", "x"]
        assert site.fetched.count("https://docs.example.com/docs/") == 1

    def test_out_of_scope_links_are_skipped_silently(self):
        site = FakeSite()
        site.responses = {
            "https://docs.example.com/docs/": html_page(
                "start", "mailto:a@b.c", "https://evil.example.com/x", "/admin/x", "/docs/ok"
            ),
            "https://docs.example.com/docs/ok": html_page("ok"),
        }
        result = crawl(start(), resolve=site.resolve, fetch=site.fetch)
        assert [page.url for page in result.pages] == [
            "https://docs.example.com/docs/",
            "https://docs.example.com/docs/ok",
        ]
        assert result.warnings == []
        assert site.fetched == [
            "https://docs.example.com/docs/",
            "https://docs.example.com/docs/ok",
        ]

    def test_start_url_outside_policy_propagates_block(self):
        with pytest.raises(BlockedUrl):
            crawl(
                start("https://evil.example.com/x"),
                resolve=FakeSite().resolve,
                fetch=FakeSite().fetch,
            )

    @pytest.mark.parametrize("addresses", [{}, {"docs.example.com": ["10.0.0.1"]}])
    def test_unsafe_destination_is_never_fetched(self, addresses):
        site = FakeSite(addresses)
        site.responses["https://docs.example.com/docs/"] = html_page("x")
        with pytest.raises(CrawlFailed):
            crawl(start(), resolve=site.resolve, fetch=site.fetch)
        assert site.fetched == []

    def test_redirect_to_allowed_page_is_followed(self):
        site = FakeSite()
        site.responses = {
            "https://docs.example.com/docs/old": FetchResponse(301, location="new-name"),
            "https://docs.example.com/docs/new-name": html_page("moved"),
        }
        result = crawl(start("https://docs.example.com/docs/old"), resolve=site.resolve, fetch=site.fetch)
        assert [page.url for page in result.pages] == [
            "https://docs.example.com/docs/new-name"
        ]
        assert [block.text for block in result.pages[0].blocks] == ["moved"]

    def test_redirect_escape_to_disallowed_host_is_refused(self):
        site = FakeSite()
        site.responses = {
            "https://docs.example.com/docs/x": FetchResponse(
                302, location="https://docs.example.com.evil.com/a"
            ),
        }
        with pytest.raises(CrawlFailed) as caught:
            crawl(start("https://docs.example.com/docs/x"), resolve=site.resolve, fetch=site.fetch)
        assert site.fetched == ["https://docs.example.com/docs/x"]
        assert "docs.example.com.evil.com" in "; ".join(caught.value.warnings)

    def test_redirect_hop_rechecked_against_private_dns(self):
        site = FakeSite({"docs.example.com": ["93.184.216.34"], "cdn.example.com": ["192.168.1.1"]})
        site.responses = {
            "https://docs.example.com/docs/x": FetchResponse(
                302, location="https://cdn.example.com/assets"
            ),
        }
        with pytest.raises(CrawlFailed):
            crawl(
                start(
                    "https://docs.example.com/docs/x",
                    allowed_hosts=["docs.example.com", "cdn.example.com"],
                    allowed_prefixes=[],
                ),
                resolve=site.resolve,
                fetch=site.fetch,
            )
        assert site.fetched == ["https://docs.example.com/docs/x"]

    def test_redirect_loop_stops_with_warning(self):
        site = FakeSite()
        site.responses = {
            "https://docs.example.com/docs/a": FetchResponse(302, location="/docs/b"),
            "https://docs.example.com/docs/b": FetchResponse(302, location="/docs/a"),
        }
        with pytest.raises(CrawlFailed) as caught:
            crawl(start("https://docs.example.com/docs/a"), resolve=site.resolve, fetch=site.fetch)
        assert "redirect limit" in "; ".join(caught.value.warnings)
        assert len(site.fetched) <= 7

    def test_partial_crawl_collects_warnings(self):
        site = FakeSite()
        site.responses = {
            "https://docs.example.com/docs/": html_page("start", "/docs/a", "/docs/b"),
            "https://docs.example.com/docs/a": TimeoutError("boom"),
            "https://docs.example.com/docs/b": FetchResponse(500, "text/html", b"<p>x</p>"),
        }
        result = crawl(start(), resolve=site.resolve, fetch=site.fetch)
        assert [page.url for page in result.pages] == ["https://docs.example.com/docs/"]
        joined = "; ".join(result.warnings)
        assert "/docs/a" in joined and "/docs/b" in joined

    def test_all_pages_failing_raises_typed_failure(self):
        site = FakeSite()
        site.responses["https://docs.example.com/docs/"] = FetchResponse(500)
        with pytest.raises(CrawlFailed) as caught:
            crawl(start(), resolve=site.resolve, fetch=site.fetch)
        assert "http 500" in str(caught.value)

    def test_depth_limit_stops_deep_links(self):
        site = FakeSite()
        urls = [f"https://docs.example.com/docs/d{i}" for i in range(7)]
        for i, url in enumerate(urls):
            linked = [f"/docs/d{i + 1}"] if i < 6 else []
            site.responses[url] = html_page(f"d{i}", *linked)
        result = crawl(start(urls[0]), resolve=site.resolve, fetch=site.fetch)
        assert [page.url for page in result.pages] == urls[:6]
        assert urls[6] not in site.fetched

    def test_page_limit_stops_crawl(self):
        site = FakeSite()
        site.responses = {
            "https://docs.example.com/docs/": html_page("start", "/docs/a", "/docs/b", "/docs/c"),
            "https://docs.example.com/docs/a": html_page("a"),
            "https://docs.example.com/docs/b": html_page("b"),
            "https://docs.example.com/docs/c": html_page("c"),
        }
        result = crawl(start(limits=CrawlLimits(max_pages=2)), resolve=site.resolve, fetch=site.fetch)
        assert [page.url for page in result.pages] == [
            "https://docs.example.com/docs/",
            "https://docs.example.com/docs/a",
        ]
        assert any("page limit" in warning for warning in result.warnings)
        assert "https://docs.example.com/docs/b" not in site.fetched

    def test_oversized_response_is_skipped(self):
        site = FakeSite()
        site.responses = {
            "https://docs.example.com/docs/": html_page("start", "/docs/big", "/docs/small"),
            "https://docs.example.com/docs/big": FetchResponse(
                200, "text/html", b"<p>" + b"x" * 200 + b"</p>"
            ),
            "https://docs.example.com/docs/small": html_page("small"),
        }
        result = crawl(
            start(limits=CrawlLimits(max_response_bytes=100)), resolve=site.resolve, fetch=site.fetch
        )
        assert [page.url for page in result.pages] == [
            "https://docs.example.com/docs/",
            "https://docs.example.com/docs/small",
        ]
        assert any("/docs/big" in warning for warning in result.warnings)

    def test_non_html_content_type_is_skipped(self):
        site = FakeSite()
        site.responses = {
            "https://docs.example.com/docs/": html_page("start", "/docs/file"),
            "https://docs.example.com/docs/file": FetchResponse(
                200, "application/pdf", b"<p>not html</p>"
            ),
        }
        result = crawl(start(), resolve=site.resolve, fetch=site.fetch)
        assert [page.url for page in result.pages] == ["https://docs.example.com/docs/"]
        assert any("not HTML" in warning for warning in result.warnings)

    def test_duplicate_links_fetch_once(self):
        site = FakeSite()
        site.responses = {
            "https://docs.example.com/docs/": html_page("start", "/docs/a", "/docs/a"),
            "https://docs.example.com/docs/a": html_page("a", "/docs/a", "/docs/"),
        }
        result = crawl(start(), resolve=site.resolve, fetch=site.fetch)
        assert [page.url for page in result.pages] == [
            "https://docs.example.com/docs/",
            "https://docs.example.com/docs/a",
        ]
        assert site.fetched.count("https://docs.example.com/docs/a") == 1

    def test_sitemap_start_harvests_locs_without_bfs(self):
        site = FakeSite()
        site.responses = {
            "https://docs.example.com/docs/sitemap.xml": FetchResponse(
                200,
                "text/xml",
                b"<?xml version='1.0'?><urlset><url>"
                b"<loc>https://docs.example.com/docs/a?q=1&amp;x=2</loc></url>"
                b"<url><loc>https://evil.example.com/nope</loc></url></urlset>",
            ),
            "https://docs.example.com/docs/a?q=1&x=2": html_page("a", "/docs/b"),
        }
        result = crawl(start("https://docs.example.com/docs/sitemap.xml"), resolve=site.resolve, fetch=site.fetch)
        assert [page.url for page in result.pages] == [
            "https://docs.example.com/docs/a?q=1&x=2"
        ]
        assert all("/docs/b" not in url for url in site.fetched)

    def test_sitemap_with_no_allowed_urls_fails(self):
        site = FakeSite()
        site.responses["https://docs.example.com/docs/sitemap.xml"] = FetchResponse(
            200,
            "text/xml",
            b"<urlset><url><loc>https://evil.example.com/nope</loc></url></urlset>",
        )
        with pytest.raises(CrawlFailed) as caught:
            crawl(start("https://docs.example.com/docs/sitemap.xml"), resolve=site.resolve, fetch=site.fetch)
        assert "sitemap" in "; ".join(caught.value.warnings)

    def test_deadline_stops_before_first_fetch(self):
        site = FakeSite()
        site.responses["https://docs.example.com/docs/"] = html_page("x")
        with pytest.raises(CrawlFailed) as caught:
            crawl(
                start(limits=CrawlLimits(max_seconds=0.0)),
                resolve=site.resolve,
                fetch=site.fetch,
                now=lambda: 2.0,
            )
        assert "time limit" in "; ".join(caught.value.warnings)
        assert site.fetched == []

    def test_failed_sitemap_download_fails_the_crawl(self):
        site = FakeSite()
        site.responses["https://docs.example.com/docs/sitemap.xml"] = FetchResponse(500)
        with pytest.raises(CrawlFailed) as caught:
            crawl(
                start("https://docs.example.com/docs/sitemap.xml"),
                resolve=site.resolve,
                fetch=site.fetch,
            )
        assert "http 500" in "; ".join(caught.value.warnings)
