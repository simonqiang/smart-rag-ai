"""Task 20c: snapshot bundle format (build/parse round trip and refusals)."""

import json

import pytest

from ingestion.parsers.basic import ExtractionFailed, RawBlock
from ingestion.parsers.snapshot import (
    SNAPSHOT_MEDIA_TYPE,
    SnapshotPage,
    encode_bundle,
    page_sha256,
    parse_snapshot,
)


def _bundle() -> bytes:
    return encode_bundle([
        SnapshotPage(url="https://docs.example.com/docs/", blocks=["start", "guide"]),
        SnapshotPage(url="https://docs.example.com/docs/zh", blocks=["智能检索助手"]),
        SnapshotPage(url="https://docs.example.com/docs/empty", blocks=[]),
    ])


def test_media_type_is_dedicated_and_not_an_upload_type() -> None:
    assert SNAPSHOT_MEDIA_TYPE.startswith("application/x-smart-rag-")
    from ingestion.uploads import MEDIA_TYPES

    assert SNAPSHOT_MEDIA_TYPE not in MEDIA_TYPES.values()


def test_parse_assigns_one_based_page_numbers_and_keeps_empty_pages() -> None:
    blocks = parse_snapshot(_bundle())

    assert [block.text for block in blocks] == ["start", "guide", "智能检索助手"]
    assert [(block.page) for block in blocks] == [1, 1, 2]
    assert blocks.warnings == []


def test_round_trip_preserves_cjk_characters() -> None:
    # ensure_ascii=False keeps CJK readable; the bundle must decode back equal.
    assert parse_snapshot(_bundle())[2].text == "智能检索助手"


def test_page_sha256_is_deterministic_and_content_sensitive() -> None:
    page = SnapshotPage(url="u", blocks=["a", "b"])
    same = SnapshotPage(url="u", blocks=["a", "b"])
    changed = SnapshotPage(url="u", blocks=["a", "c"])
    different_url = SnapshotPage(url="v", blocks=["a", "b"])

    assert page_sha256(page) == page_sha256(same)
    assert page_sha256(page) != page_sha256(changed)
    # Content identity, not URL identity: the URL is stored beside the sha.
    assert page_sha256(page) == page_sha256(different_url)


def test_bundle_is_canonical_json() -> None:
    # Byte-identical re-encodings are what make unchanged-sha reuse work.
    assert _bundle() == _bundle()
    payload = json.loads(_bundle())
    assert _bundle() == json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@pytest.mark.parametrize("payload", [
    b"not json at all",
    b"{}",
    b'{"version": 2, "pages": []}',
    b'{"version": 1, "pages": {}}',
    b'{"version": 1, "pages": [{}]}',
    b'{"version": 1, "pages": [{"url": 3, "blocks": []}]}',
    b'{"version": 1, "pages": [{"url": "u", "blocks": "nope"}]}',
    b'{"version": 1, "pages": [{"url": "u", "blocks": [1]}]}',
    b'{"version": 1}',
])
def test_malformed_bundles_fail_with_a_typed_reason(payload: bytes) -> None:
    with pytest.raises(ExtractionFailed) as caught:
        parse_snapshot(payload)
    assert caught.value.reason == "corrupt_snapshot"


def test_parse_snapshot_returns_plain_blocks_parseable_as_raw() -> None:
    blocks = parse_snapshot(_bundle())

    assert isinstance(blocks[0], RawBlock)
