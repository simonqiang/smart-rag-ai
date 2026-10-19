"""Website crawler (Task 20b): policy-checked sitemap/BFS fetching.

All I/O sits behind the ``resolve``/``fetch`` seams so security tests prove
zero private-network escape offline; the production httpx transport plugs in
at the worker boundary. Every URL — including each redirect hop — is
re-validated through the ``CrawlPolicy`` and its destination re-resolved
before a request goes out.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from ingestion.parsers.basic import RawBlock
from ingestion.parsers.html import _decode_declared, parse_html
from web_sync.crawl_policy import BlockedUrl, CrawlPolicy

MAX_REDIRECTS = 5
_HTML_TYPES = frozenset(("text/html", "application/xhtml+xml"))
_LOC = re.compile(r"<loc>(.*?)</loc>", re.DOTALL)


class CrawlFailed(Exception):
    """Every attempted page failed; ``warnings`` carries the per-URL reasons."""

    def __init__(self, warnings: list[str]) -> None:
        joined = "; ".join(warnings) or "nothing fetched"
        super().__init__(f"crawl produced no pages: {joined}")
        self.warnings = warnings


@dataclass(frozen=True)
class CrawlDefinition:
    start_url: str
    policy: CrawlPolicy


@dataclass(frozen=True)
class FetchResponse:
    status: int
    content_type: str = ""
    body: bytes = b""
    location: str | None = None  # redirect target when 300 <= status < 400


@dataclass(frozen=True)
class CrawledPage:
    url: str
    blocks: list[RawBlock]


@dataclass
class CrawlResult:
    pages: list[CrawledPage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def crawl(
    definition: CrawlDefinition,
    *,
    resolve: Callable[[str], list[str]],
    fetch: Callable[[str], FetchResponse],
    now: Callable[[], float] = time.monotonic,
) -> CrawlResult:
    policy = definition.policy
    limits = policy.limits
    start = policy.validate_url(definition.start_url)
    result = CrawlResult()
    deadline = now() + limits.max_seconds

    def download(url: str) -> tuple[str, bytes, str] | None:
        """Resolve, fetch, and follow redirects; returns (final URL, body,
        content type) or None on refusal."""
        for _ in range(MAX_REDIRECTS + 1):
            host = urlsplit(url).hostname or ""
            try:
                policy.check_addresses(host, resolve(host))
                response = fetch(url)
            except BlockedUrl as blocked:
                result.warnings.append(f"{url}: {blocked}")
                return None
            except Exception as error:  # noqa: BLE001 - per-URL failures warn, not crash
                result.warnings.append(f"{url}: {error}")
                return None
            if 300 <= response.status < 400 and response.location:
                try:
                    url = policy.validate_url(urljoin(url, response.location))
                except BlockedUrl as blocked:
                    result.warnings.append(f"{url}: redirect {blocked}")
                    return None
                continue
            if response.status != 200:
                result.warnings.append(f"{url}: http {response.status}")
                return None
            if len(response.body) > limits.max_response_bytes:
                result.warnings.append(f"{url}: response exceeds size limit")
                return None
            return url, response.body, response.content_type
        result.warnings.append(f"{url}: redirect limit exceeded")
        return None

    sitemap = urlsplit(start).path.endswith(".xml")
    queue: deque[tuple[str, int]] = deque()
    if sitemap:
        downloaded = download(start)
        if downloaded is not None:
            for loc in _LOC.findall(_decode_declared(downloaded[1])):
                try:
                    queue.append((policy.validate_url(unescape(loc.strip())), 0))
                except BlockedUrl:
                    pass  # sitemaps list foreign URLs by design
            if not queue:
                result.warnings.append(f"{start}: sitemap lists no allowed URLs")
    else:
        queue.append((start, 0))

    seen: set[str] = set()
    while queue:
        url, depth = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        if len(result.pages) >= limits.max_pages:
            result.warnings.append(f"page limit {limits.max_pages} reached")
            break
        if now() >= deadline:
            result.warnings.append("time limit reached")
            break
        downloaded = download(url)
        if downloaded is None:
            continue
        final_url, body, content_type = downloaded
        if content_type.split(";")[0].strip().lower() not in _HTML_TYPES:
            result.warnings.append(f"{url}: not HTML ({content_type})")
            continue
        result.pages.append(CrawledPage(final_url, parse_html(body)))
        if not sitemap and depth < limits.max_depth:
            for candidate in _links(_decode_declared(body), final_url):
                try:
                    candidate = policy.validate_url(candidate)
                except BlockedUrl:
                    continue  # out-of-scope links are normal, not failures
                if candidate not in seen:
                    queue.append((candidate, depth + 1))

    if not result.pages:
        raise CrawlFailed(result.warnings)
    return result


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.hrefs.append(value)


def _links(body: str, base: str) -> list[str]:
    collector = _LinkCollector()
    collector.feed(body)
    collector.close()
    return [urljoin(base, href.strip()) for href in collector.hrefs]
