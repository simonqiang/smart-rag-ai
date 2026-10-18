"""Business document and OCR adapters (Task 19): HTML, DOCX, PPTX, XLSX, CSV, images.

Parser-level tests run in-memory; upload acceptance runs against the live
Compose database through the shared ingestion fixtures.
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.events import EventWriter
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext
from ingestion.extraction import SourceObject, extract


def _extract(media_type: str, data: bytes, filename: str) -> object:
    return extract(
        SourceObject(
            source_version_id="v1", media_type=media_type, filename=filename, data=data
        )
    )


def _html(data: bytes) -> object:
    return _extract("text/html", data, "page.html")


# --------------------------------------------------------------------------
# HTML (19a)
# --------------------------------------------------------------------------


def test_html_scripts_styles_and_templates_are_dropped() -> None:
    doc = _html(
        b"<html><head><script>alert(1)</script><style>p { color: red }</style>"
        b"</head><body><template><p>ghost</p></template>"
        b"<h1>Policy</h1><p>Leave accrues monthly.</p>"
        b"<noscript>enable javascript</noscript></body></html>"
    )

    assert [block.text for block in doc.blocks] == ["Policy", "Leave accrues monthly."]
    assert doc.warnings == []


def test_html_nav_header_footer_aside_boilerplate_is_skipped() -> None:
    doc = _html(
        b"<html><body><header>ACME Inc</header>"
        b"<nav><header>brand</header><ul><li>Home</li><li>Products</li></ul></nav>"
        b"<article><p>Real content stays.</p></article>"
        b"<aside>Advertisement</aside><footer>Copyright ACME</footer></body></html>"
    )

    assert [block.text for block in doc.blocks] == ["Real content stays."]


def test_html_blocks_are_ordered_with_ordinal_locations() -> None:
    doc = _html(
        b"<html><body><h1>Handbook</h1>"
        b"<p>First paragraph across<br>two lines.</p>"
        b"<ul><li>Alpha</li><li>Beta</li></ul></body></html>"
    )

    assert [block.text for block in doc.blocks] == [
        "Handbook",
        "First paragraph across two lines.",
        "Alpha",
        "Beta",
    ]
    assert [block.location.block for block in doc.blocks] == [0, 1, 2, 3]
    assert all(block.location.page is None for block in doc.blocks)


def test_html_gb18030_meta_charset_decodes_chinese() -> None:
    body = (
        '<html><head><meta charset="gb18030"></head>'
        "<body><h1>公司介绍</h1><p>员工每年享有十五天年假。</p></body></html>"
    ).encode("gb18030")
    doc = _html(body)

    assert [block.text for block in doc.blocks] == [
        "公司介绍",
        "员工每年享有十五天年假。",
    ]
    assert doc.blocks[-1].language == "zh-Hans"


def test_html_undeclared_utf8_chinese_still_decodes() -> None:
    doc = _html("<html><body><p>員工享有年假。</p></body></html>".encode())

    assert doc.blocks[0].text == "員工享有年假。"
    assert doc.blocks[0].language == "zh-Hant"


def test_html_malformed_markup_still_extracts_readable_text() -> None:
    doc = _html(
        b"<html><body></script><p>Unclosed <b>bold and <i>more text</p><p>Next</b></p>"
    )

    assert [block.text for block in doc.blocks] == [
        "Unclosed bold and more text",
        "Next",
    ]


def test_html_undecodable_bytes_fail_typed() -> None:
    from ingestion.parsers.basic import ExtractionFailed

    with pytest.raises(ExtractionFailed) as excinfo:
        _html(b"\xff\xfe\xff\x00\x81\x81 raw junk")
    assert excinfo.value.reason == "decode_failed"


def test_html_script_only_page_warns_empty() -> None:
    doc = _html(
        b"<html><head><title></title></head><body><script>var x=1;</script></body></html>"
    )

    assert doc.blocks == []
    assert doc.warnings == ["empty"]


def test_html_entities_and_comments_render_as_text() -> None:
    doc = _html(
        b"<html><body><p>Profit &amp; loss &lt;2026&gt;</p><!-- hidden --></body></html>"
    )

    assert doc.blocks[0].text == "Profit & loss <2026>"


def test_html_wrong_declared_charset_falls_back_to_detection() -> None:
    content = "<html><body><p>員工享有年假。</p></body></html>".encode()  # really UTF-8
    doc = _html(content.replace(b"<body>", b'<body><meta charset="iso-8859-1">'))
    doc_late_meta = _html(
        content.replace(
            b"<body>", b'<body><meta http-equiv="content-type" content="text/html; charset=gb18030">'
        )
    )

    assert doc.blocks[0].text == "員工享有年假。"
    assert doc_late_meta.blocks[0].text == "員工享有年假。"
    assert doc.blocks[0].language == "zh-Hant"


def test_html_blank_block_content_is_dropped() -> None:
    doc = _html(b"<html><body><p>Real</p><p>   </p><p>Also real</p></body></html>")

    assert [block.text for block in doc.blocks] == ["Real", "Also real"]


def test_html_registered_in_extraction_registry() -> None:
    doc = _extract("text/html", b"<html><body><p>hello</p></body></html>", "page.html")

    assert doc.media_type == "text/html"
    assert doc.blocks[0].text == "hello"


def test_html_upload_accepted_and_stored(db, store_root, workspace) -> None:
    from ingestion.uploads import register_upload

    async def run(uow, store: ObjectStore) -> object:
        return await register_upload(
            uow,
            EventWriter(),
            store,
            context=AccessContext(
                user_id=workspace["owner_id"],
                workspace_id=workspace["workspace_id"],
                role="owner",
            ),
            collection_id=workspace["collection_id"],
            name="Landing page",
            filename="page.html",
            data=b"<html><body><h1>Hello</h1></body></html>",
        )

    async def run_all() -> object:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            return await run(uow, ObjectStore(store_root))
        finally:
            await engine.dispose()

    accepted = asyncio.run(run_all())

    assert accepted.media_type == "text/html"
