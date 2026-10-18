"""Crawl policy (Task 20a): URL admission and destination safety.

Pure logic only — no I/O. The crawler resolves DNS and fetches through its
own injectable seams and hands resolved addresses to ``check_addresses``
before every request, including each redirect hop. Crawl limits are the
owner-confirmed spec §15 values.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class CrawlLimits:
    max_pages: int = 500
    max_depth: int = 5
    max_seconds: float = 1800.0
    max_response_bytes: int = 10 * 1024 * 1024


@dataclass(frozen=True)
class BlockedUrl(Exception):
    """A URL or resolved destination the policy refuses, with the reason."""

    url: str
    reason: str

    def __str__(self) -> str:
        return f"blocked ({self.reason}): {self.url}"


def _is_non_global(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Explicit flags rather than is_global: their semantics drifted across
    # Python versions and the deny list must stay deterministic. IPv4-mapped
    # IPv6 is unwrapped so ::ffff:10.0.0.1 cannot smuggle a private v4 target.
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


class CrawlPolicy:
    """Admits URLs within the allowed hosts/prefixes and refuses private
    network destinations."""

    def __init__(
        self,
        *,
        allowed_hosts: list[str],
        allowed_prefixes: list[str] | None = None,
        limits: CrawlLimits | None = None,
    ) -> None:
        self.allowed_hosts = {host.lower() for host in allowed_hosts}
        # ponytail: normalization lowercases the host and drops the fragment
        # and default port only; add percent-encoding/unicode canon if real
        # sites ever dedupe differently.
        self.allowed_prefixes = tuple(allowed_prefixes or ())
        self.limits = limits or CrawlLimits()

    def validate_url(self, url: str) -> str:
        """Return the normalized form of ``url`` or raise ``BlockedUrl``."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise BlockedUrl(url, "scheme")
        if parts.username is not None or parts.password is not None:
            raise BlockedUrl(url, "userinfo")
        hostname = (parts.hostname or "").lower()
        if not hostname or hostname not in self.allowed_hosts:
            raise BlockedUrl(url, "host")
        path = parts.path or "/"
        if self.allowed_prefixes and not path.startswith(self.allowed_prefixes):
            raise BlockedUrl(url, "path")
        port = parts.port
        default = (parts.scheme == "http" and port == 80) or (
            parts.scheme == "https" and port == 443
        )
        netloc = hostname if port is None or default else f"{hostname}:{port}"
        return urlunsplit((parts.scheme, netloc, path, parts.query, ""))

    def check_addresses(self, host: str, addresses: list[str]) -> None:
        """Refuse unless ``host`` resolved to at least one global address and
        every resolved address is global (a mixed answer is not trustworthy)."""
        if not addresses:
            raise BlockedUrl(host, "unresolved")
        for raw in addresses:
            if _is_non_global(ipaddress.ip_address(raw)):
                raise BlockedUrl(host, "private_network")
