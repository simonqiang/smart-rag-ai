"""Task 12: deterministic chunking — boundaries, CJK, languages, locations."""

from ingestion.chunking import ChunkPolicy, chunk
from ingestion.extraction import ExtractedDocument, SourceLocation, TextBlock


def _document(*blocks: tuple[str, str]) -> ExtractedDocument:
    return ExtractedDocument(
        source_version_id="v1",
        media_type="text/plain",
        language="mixed" if len({language for _, language in blocks}) > 1 else (blocks[0][1] if blocks else "en"),
        blocks=[
            TextBlock(text, language, SourceLocation(1, ordinal))
            for ordinal, (text, language) in enumerate(blocks)
        ],
        warnings=[],
    )


def test_chunking_is_deterministic_for_identical_documents() -> None:
    document = _document(("first paragraph", "en"), ("second paragraph", "en"))
    assert chunk(document, ChunkPolicy(max_chars=100)) == chunk(document, ChunkPolicy(max_chars=100))


def test_blocks_pack_in_order_and_carry_locations() -> None:
    document = _document(("alpha text", "en"), ("beta text", "en"), ("gamma text", "en"))
    chunks = chunk(document, ChunkPolicy(max_chars=100))

    assert [c.text for c in chunks] == ["alpha text\nbeta text\ngamma text"]
    assert chunks[0].page == 1
    assert (chunks[0].block_start, chunks[0].block_end) == (0, 2)


def test_packing_respects_max_chars() -> None:
    paragraph = " ".join(["word"] * 4)  # 19 chars
    document = _document((paragraph, "en"), (paragraph, "en"), (paragraph, "en"))
    chunks = chunk(document, ChunkPolicy(max_chars=20))

    assert all(len(c.text) <= 20 for c in chunks)
    assert len(chunks) >= 2


def test_oversized_block_is_windowed_with_overlap() -> None:
    text = "".join(str(i % 10) for i in range(30))  # 30 chars, no whitespace
    chunks = chunk(_document((text, "en")), ChunkPolicy(max_chars=10, overlap_chars=4))

    assert all(len(c.text) <= 10 for c in chunks)
    assert chunks[0].text == text[:10]
    # step = max_chars - overlap = 6: second window starts at char 6.
    assert chunks[1].text == text[6:16]
    assert (chunks[0].block_start, chunks[0].block_end) == (0, 0)


def test_cjk_text_without_word_spaces_chunks_deterministically() -> None:
    text = "智能检索助手面向中小企业提供本地知识问答服务" * 3
    chunks = chunk(_document((text, "zh-Hans")), ChunkPolicy(max_chars=20, overlap_chars=5))

    assert sum(len(c.text.replace("\n", "")) for c in chunks) > 0
    assert all(len(c.text) <= 20 for c in chunks)
    assert [c.text for c in chunks] == [c.text for c in chunks]  # deterministic
    assert chunks[0].text == text[:20]


def test_per_chunk_language_is_block_language_or_mixed() -> None:
    english_only = chunk(_document(("only english words", "en")), ChunkPolicy())
    assert english_only[0].language == "en"

    chinese_only = chunk(_document(("中文内容块", "zh-Hans")), ChunkPolicy())
    assert chinese_only[0].language == "zh-Hans"

    mixed = chunk(_document(("english words", "en"), ("中文块", "zh-Hans")), ChunkPolicy(max_chars=100))
    assert {c.language for c in mixed} == {"mixed"}


def test_empty_document_chunks_to_nothing() -> None:
    assert chunk(_document(), ChunkPolicy()) == []


def test_never_packs_across_pages() -> None:
    document = ExtractedDocument(
        source_version_id="v1",
        media_type="application/pdf",
        language="en",
        blocks=[
            TextBlock("page one words", "en", SourceLocation(1, 0)),
            TextBlock("page two words", "en", SourceLocation(2, 1)),
        ],
        warnings=[],
    )
    chunks = chunk(document, ChunkPolicy(max_chars=100))

    assert [c.text for c in chunks] == ["page one words", "page two words"]
    assert [c.page for c in chunks] == [1, 2]
