"""Website snapshot bundle format (Task 20c).

A website crawl is stored as ONE source version: canonical JSON carrying the
crawled pages in crawl order, each with its URL, a per-page content sha256
(Task 21 unchanged-page detection), and the parsed text blocks. The shared
HTML parser runs exactly once at crawl time, so the bundle stores parser
output, not raw HTML. ``web_sync.snapshots`` builds bundles with
:func:`encode_bundle`; :func:`parse_snapshot` reads them back into located
blocks so extraction and indexing treat a snapshot like any other document.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .basic import ExtractionFailed, ParseResult, RawBlock

# Dedicated media type; deliberately NOT an upload media type — bundles are
# server-generated, and admitting them as uploads would let clients POST
# hostile bundles as "snapshots".
SNAPSHOT_MEDIA_TYPE = "application/x-smart-rag-website-snapshot"

BUNDLE_VERSION = 1


@dataclass(frozen=True)
class SnapshotPage:
    url: str
    blocks: list[str]


def page_sha256(page: SnapshotPage) -> str:
    """Checksum over the normalized page content (block text, URL excluded)."""
    return hashlib.sha256("\n".join(page.blocks).encode("utf-8")).hexdigest()


def encode_bundle(pages: list[SnapshotPage]) -> bytes:
    payload = {
        "version": BUNDLE_VERSION,
        "pages": [
            {"url": page.url, "sha256": page_sha256(page), "blocks": page.blocks}
            for page in pages
        ],
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def parse_snapshot(data: bytes) -> ParseResult:
    try:
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != BUNDLE_VERSION:
            raise ValueError("unsupported bundle version")
        entries = payload["pages"]
        if not isinstance(entries, list):
            raise TypeError("pages must be a list")
        blocks: list[RawBlock] = []
        for number, entry in enumerate(entries, start=1):
            url, texts = entry["url"], entry["blocks"]
            if not isinstance(url, str) or not isinstance(texts, list):
                raise TypeError("page shape")
            if not all(isinstance(text, str) for text in texts):
                raise TypeError("block shape")
            blocks.extend(RawBlock(text, page=number) for text in texts)
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as failure:
        raise ExtractionFailed("corrupt_snapshot") from failure
    return ParseResult(blocks, [])
