"""Task 11: basic parsers (TXT/Markdown/PDF) and the extract worker.

All parsing runs offline: text decoding goes through charset-normalizer and
PDF text comes from Docling's parse backend, so the blocking suite stays
deterministic with no network. The worker section drives the real job
plumbing (claims, leases, state flips) against live PostgreSQL.
"""

import asyncio
import threading
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.jobs import JobCommand, JobQueue
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext
from ingestion.extraction import (
    ExtractedDocument,
    ExtractionFailed,
    SourceObject,
    detect_language,
    extract,
)
from ingestion.parsers.basic import parse_pdf
from ingestion.uploads import register_upload

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"


def _run(settings: Settings, coro_factory, store_root: Path | None = None) -> object:
    async def run() -> object:
        engine = create_async_engine(settings.database_url)
        uow = UnitOfWork(engine)
        store = ObjectStore(store_root) if store_root else None
        try:
            return await coro_factory(uow, store)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _owner_context(workspace: dict) -> AccessContext:
    return AccessContext(
        user_id=workspace["owner_id"], workspace_id=workspace["workspace_id"], role="owner"
    )


def _upload(db: Settings, store_root: Path, workspace: dict, *, data: bytes,
            filename: str) -> object:
    async def run(uow, store: ObjectStore) -> object:
        return await register_upload(
            uow, EventWriter(), store,
            context=_owner_context(workspace),
            collection_id=workspace["collection_id"],
            name="Test document",
            filename=filename,
            data=data,
        )

    return _run(db, run, store_root)


# --------------------------------------------------------------------------
# Language detection (unit-level, exercised through extraction)
# --------------------------------------------------------------------------


def test_detect_language_strata() -> None:
    assert detect_language("Welcome to the company handbook for all staff.") == "en"
    assert detect_language("员工每年享有十五天年假。") == "zh-Hans"
    assert detect_language("員工每年享有十五天年假。") == "zh-Hant"
    assert detect_language(
        "Buku panduan ini menerangkan cuti tahunan dan waktu kerja untuk semua kakitangan."
    ) == "ms"
    assert detect_language(
        "产品发布会 Product Launch 定于十月十日举行。Selamat datang ke acara tahunan."
    ) == "mixed"


def test_detect_language_defaults_to_english_without_signal() -> None:
    assert detect_language("12345 ???") == "en"


# --------------------------------------------------------------------------
# Text and Markdown extraction
# --------------------------------------------------------------------------


def _extract_file(filename: str, media_type: str) -> ExtractedDocument:
    data = (FIXTURES / "text" / filename).read_bytes()
    return extract(SourceObject(
        source_version_id="v1", media_type=media_type, filename=filename, data=data
    ))


def test_english_text_ordered_blocks_with_locations() -> None:
    doc = _extract_file("notes-en.txt", "text/plain")

    assert doc.warnings == []
    assert doc.language == "en"
    assert [block.text for block in doc.blocks] == [
        ("Welcome to the company. This handbook explains leave policies "
         "and working hours for all staff.")
    ]
    assert doc.blocks[0].location.page is None
    assert doc.blocks[0].location.block == 0
    assert doc.blocks[0].language == "en"


def test_wrapped_lines_join_cjk_aware() -> None:
    doc = extract(SourceObject(
        source_version_id="v1", media_type="text/plain", filename="wrap.txt",
        data="Hello world\nwrapped here\n\n第二段开始\n换行继续".encode(),
    ))

    assert [block.text for block in doc.blocks] == [
        "Hello world wrapped here", "第二段开始换行继续"
    ]
    assert [block.location.block for block in doc.blocks] == [0, 1]


def test_gb18030_fixture_decodes_to_simplified_chinese() -> None:
    doc = _extract_file("notes-zh-hans.gb18030.txt", "text/plain")

    assert doc.blocks[0].text.startswith("员工每年")
    assert doc.blocks[0].language == "zh-Hans"


def test_big5_fixture_decodes_to_traditional_chinese() -> None:
    doc = _extract_file("notes-zh-hant.big5.txt", "text/plain")

    assert doc.blocks[0].text.startswith("員工每年")
    assert doc.blocks[0].language == "zh-Hant"


def test_utf8_chinese_variants_are_script_tagged() -> None:
    hans = _extract_file("notes-zh-hans.txt", "text/plain")
    hant = _extract_file("notes-zh-hant.txt", "text/plain")

    assert hans.blocks[0].language == "zh-Hans"
    assert hant.blocks[0].language == "zh-Hant"


def test_mixed_content_block_is_tagged_mixed() -> None:
    doc = _extract_file("notes-mixed.txt", "text/plain")

    assert doc.blocks[0].language == "mixed"
    assert doc.language == "mixed"


def test_undecodable_bytes_fail_typed() -> None:
    with pytest.raises(ExtractionFailed) as excinfo:
        extract(SourceObject(
            source_version_id="v1", media_type="text/plain", filename="junk.txt",
            data=b"\xff\xff\xff\xff\xff",
        ))

    assert excinfo.value.reason == "decode_failed"


def test_empty_text_yields_empty_warning() -> None:
    doc = extract(SourceObject(
        source_version_id="v1", media_type="text/plain", filename="empty.txt", data=b"",
    ))

    assert doc.blocks == []
    assert doc.warnings == ["empty"]


def test_markdown_headings_and_paragraphs_ordered() -> None:
    doc = _extract_file("handbook.md", "text/markdown")

    texts = [block.text for block in doc.blocks]
    assert texts[0] == "Employee Handbook"
    assert texts[1] == "Welcome to the company. This handbook explains leave policies."
    assert texts[2] == "Annual leave"
    assert texts[3] == "员工每年享有十五天年假。年假必须提前申请。"
    assert "borang yang betul" in texts[4]
    assert "Cuti akan diluluskan" in texts[4]  # list items stay one block
    assert [block.location.block for block in doc.blocks] == [0, 1, 2, 3, 4]
    assert [block.language for block in doc.blocks][:4] == ["en", "en", "en", "zh-Hans"]
    assert doc.blocks[4].language == "ms"


def test_unsupported_media_type_fails_typed() -> None:
    with pytest.raises(ExtractionFailed) as excinfo:
        extract(SourceObject(
            source_version_id="v1", media_type="application/zip", filename="a.zip",
            data=b"PK",
        ))

    assert excinfo.value.reason == "unsupported_type"


# --------------------------------------------------------------------------
# PDF extraction (Docling parse backend, offline)
# --------------------------------------------------------------------------


def _pdf(name: str) -> bytes:
    return (FIXTURES / "ingestion" / f"{name}.pdf").read_bytes()


def test_pdf_multilingual_blocks_ordered_by_page() -> None:
    doc = extract(SourceObject(
        source_version_id="v1", media_type="application/pdf",
        filename="multilingual.pdf", data=_pdf("multilingual"),
    ))

    assert doc.warnings == []
    pages = [block.location.page for block in doc.blocks]
    assert pages == sorted(pages)
    assert pages[0] == 1
    texts = [block.text for block in doc.blocks]
    assert "Employee Handbook" in texts
    assert doc.language == "mixed"
    languages = {block.language for block in doc.blocks}
    assert {"en", "zh-Hans", "ms"} <= languages


def test_pdf_stitches_wrapped_lines_within_paragraph() -> None:
    doc = extract(SourceObject(
        source_version_id="v1", media_type="application/pdf",
        filename="multilingual.pdf", data=_pdf("multilingual"),
    ))

    stitched = [block.text for block in doc.blocks
                if block.text.startswith("Permohonan")]
    assert stitched == [
        ("Permohonan cuti tahunan hendaklah dikemukakan sekurang-kurangnya "
         "tujuh hari lebih awal.")
    ]


def test_pdf_empty_document_warns_empty() -> None:
    doc = extract(SourceObject(
        source_version_id="v1", media_type="application/pdf",
        filename="empty.pdf", data=_pdf("empty"),
    ))

    assert doc.blocks == []
    assert doc.warnings == ["empty"]


def test_pdf_scanned_document_warns_empty() -> None:
    doc = extract(SourceObject(
        source_version_id="v1", media_type="application/pdf",
        filename="scanned.pdf", data=_pdf("scanned"),
    ))

    assert doc.blocks == []
    assert doc.warnings == ["empty"]


def test_pdf_encrypted_fails_typed() -> None:
    with pytest.raises(ExtractionFailed) as excinfo:
        extract(SourceObject(
            source_version_id="v1", media_type="application/pdf",
            filename="encrypted.pdf", data=_pdf("encrypted"),
        ))

    assert excinfo.value.reason == "encrypted_pdf"


def test_pdf_malformed_fails_typed() -> None:
    with pytest.raises(ExtractionFailed) as excinfo:
        extract(SourceObject(
            source_version_id="v1", media_type="application/pdf",
            filename="broken.pdf", data=b"%PDF-1.7 not really a pdf",
        ))

    assert excinfo.value.reason == "corrupt_pdf"


def test_pdf_without_magic_fails_typed() -> None:
    with pytest.raises(ExtractionFailed) as excinfo:
        parse_pdf(b"hello, not a pdf")

    assert excinfo.value.reason == "corrupt_pdf"


def test_pdf_backend_crash_is_mapped_to_corrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> object:
        raise ValueError("docling exploded")

    monkeypatch.setattr("docling.datamodel.document.InputDocument", explode)
    with pytest.raises(ExtractionFailed) as excinfo:
        parse_pdf(_pdf("multilingual"))

    assert excinfo.value.reason == "corrupt_pdf"


def test_pdf_invalid_page_fails_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    from docling.backend.docling_parse_backend import (
        ThreadedDoclingParseDocumentBackend,
    )

    class BrokenPage:
        def is_valid(self) -> bool:
            return False

    monkeypatch.setattr(
        ThreadedDoclingParseDocumentBackend, "iter_pages", lambda self: iter([BrokenPage()])
    )
    with pytest.raises(ExtractionFailed) as excinfo:
        parse_pdf(_pdf("multilingual"))

    assert excinfo.value.reason == "corrupt_pdf"


def test_markdown_blank_lines_do_not_become_blocks() -> None:
    doc = extract(SourceObject(
        source_version_id="v1", media_type="text/markdown", filename="m.md",
        data=b"\n\n# Title\nplain line in heading chunk\n\n\nbody\n\n",
    ))

    assert [block.text for block in doc.blocks] == ["Title", "body"]


def test_pdf_over_page_limit_fails_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    from ingestion.parsers import basic

    monkeypatch.setattr(basic, "MAX_PDF_PAGES", 1)
    with pytest.raises(ExtractionFailed) as excinfo:
        parse_pdf(_pdf("multilingual"))

    assert excinfo.value.reason == "too_many_pages"


# --------------------------------------------------------------------------
# Worker: idempotent state transitions with retry
# --------------------------------------------------------------------------


def _enqueue_and_payload(db: Settings, accepted, store_root: Path) -> dict:
    async def run(uow, _store) -> dict:
        async with uow.transaction() as transaction:
            job_id = await JobQueue().enqueue(
                JobCommand(
                    type="source_extract",
                    payload={
                        "version_id": accepted.source_version_id,
                        "source_id": accepted.source_id,
                    },
                    idempotency_key=f"source_extract:{accepted.source_version_id}",
                ),
                transaction,
            )
        return {"job_id": str(job_id), "version_id": accepted.source_version_id,
                "source_id": accepted.source_id}

    return _run(db, run, store_root)


def _worker(db: Settings, store_root: Path, payload: dict) -> str:
    from apps.worker.tasks.extract import extract_version

    async def run() -> str:
        engine = create_async_engine(db.database_url)
        try:
            return await extract_version(engine, ObjectStore(store_root), payload)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _version_state(db: Settings, job_id: str) -> tuple[str, list[str], str, str]:
    async def run(uow, _store) -> tuple[str, list[str], str, str]:
        async with uow.transaction() as transaction:
            version = (
                await transaction.execute(text("SELECT state FROM source_versions"))
            ).scalar_one()
            audits = (
                await transaction.execute(
                    text("SELECT type FROM audit_events WHERE type LIKE 'source.%' "
                         "ORDER BY created_at, type")
                )
            ).scalars().all()
            job = (
                await transaction.execute(
                    text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id}
                )
            ).scalar_one()
            source = (
                await transaction.execute(text("SELECT state FROM sources"))
            ).scalar_one()
        return version, list(audits), job, source

    return _run(db, run)


def test_worker_extracts_and_marks_version_with_audit(db, workspace, store_root) -> None:
    accepted = _upload(db, store_root, workspace,
                       data=_pdf("multilingual"), filename="multilingual.pdf")
    payload = _enqueue_and_payload(db, accepted, store_root)

    assert _worker(db, store_root, payload) == "extracted"
    version, audits, job, source = _version_state(db, payload["job_id"])
    assert version == "extracted"
    assert job == "completed"
    assert source == "active"
    assert audits[-1] == "source.extracted"


def test_worker_failure_marks_version_failed_and_leaves_source_active(
    db, workspace, store_root,
) -> None:
    accepted = _upload(db, store_root, workspace,
                       data=b"%PDF-1.7 fake pdf body for testing", filename="broken.pdf")
    payload = _enqueue_and_payload(db, accepted, store_root)

    outcome = _worker(db, store_root, payload)

    assert outcome == "failed:corrupt_pdf"
    version, audits, job, source = _version_state(db, payload["job_id"])
    assert version == "failed"
    assert job == "failed"
    assert source == "active"
    assert "source.extracted" not in audits
    assert audits[-1] == "source.extraction_failed"


def test_worker_transient_error_keeps_uploaded_state_and_retry_recovers(
    db, workspace, store_root,
) -> None:
    accepted = _upload(db, store_root, workspace,
                       data=_pdf("multilingual"), filename="multilingual.pdf")
    payload = _enqueue_and_payload(db, accepted, store_root)
    object_path = store_root / "objects" / accepted.checksum
    real_bytes = object_path.read_bytes()

    # Corrupt the stored object: store.get fails with a transient error.
    object_path.write_bytes(b"poisoned")
    with pytest.raises(ValueError, match="checksum mismatch"):
        _worker(db, store_root, payload)
    version, _audits, job, _source = _version_state(db, payload["job_id"])
    assert version == "uploaded"
    assert job == "running"

    # Redelivery after the lease expires finds a consistent object.
    object_path.write_bytes(real_bytes)

    async def expire_lease() -> None:
        engine = create_async_engine(db.database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE jobs SET lease_expires_at = now() - interval '1 second'")
            )
        await engine.dispose()

    asyncio.run(expire_lease())
    assert _worker(db, store_root, payload) == "extracted"
    version, _audits, job, _source = _version_state(db, payload["job_id"])
    assert version == "extracted"
    assert job == "completed"


def test_worker_completed_delivery_is_idempotent(db, workspace, store_root) -> None:
    accepted = _upload(db, store_root, workspace,
                       data=_pdf("multilingual"), filename="multilingual.pdf")
    payload = _enqueue_and_payload(db, accepted, store_root)

    assert _worker(db, store_root, payload) == "extracted"
    assert _worker(db, store_root, payload) == "skipped"
    version, audits, job, _source = _version_state(db, payload["job_id"])
    assert version == "extracted"
    assert job == "completed"
    assert audits.count("source.extracted") == 1


def test_worker_crash_between_version_and_job_completion_is_recovered(
    db, workspace, store_root,
) -> None:
    # The version flipped but the job record did not: redelivery must not
    # re-extract or double-audit.
    accepted = _upload(db, store_root, workspace,
                       data=_pdf("multilingual"), filename="multilingual.pdf")
    payload = _enqueue_and_payload(db, accepted, store_root)
    assert _worker(db, store_root, payload) == "extracted"

    async def reset_job() -> None:
        engine = create_async_engine(db.database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE jobs SET status = 'pending', lease_owner = NULL, "
                     "lease_expires_at = NULL WHERE id = :id"),
                {"id": payload["job_id"]},
            )
        await engine.dispose()

    asyncio.run(reset_job())
    assert _worker(db, store_root, payload) == "already_extracted"
    version, audits, job, _source = _version_state(db, payload["job_id"])
    assert version == "extracted"
    assert job == "completed"
    assert audits.count("source.extracted") == 1


def test_actor_composes_settings_store_and_pipeline(
    db, workspace, store_root, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.worker.tasks import extract as task_module

    accepted = _upload(db, store_root, workspace,
                       data=_pdf("multilingual"), filename="multilingual.pdf")
    payload = _enqueue_and_payload(db, accepted, store_root)

    class FakeSettings:
        database_url = db.database_url
        data_dir = str(store_root)

        @classmethod
        def load(cls) -> "FakeSettings":
            return cls()

    monkeypatch.setattr(task_module, "Settings", FakeSettings)
    monkeypatch.setattr(task_module, "ObjectStore", lambda _root: ObjectStore(store_root))

    assert task_module.source_extract(**payload) == "extracted"
    version, _audits, job, _source = _version_state(db, payload["job_id"])
    assert version == "extracted"
    assert job == "completed"


def test_worker_aborts_when_job_is_cancelled_after_claim(
    db, workspace, store_root, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.worker.tasks import extract as task_module

    accepted = _upload(db, store_root, workspace,
                       data=_pdf("multilingual"), filename="multilingual.pdf")
    payload = _enqueue_and_payload(db, accepted, store_root)

    def cancel_during_extract(source_object: SourceObject) -> ExtractedDocument:
        # Simulate a concurrent cancel request landing while the worker is
        # mid-extraction: its own event loop, like the API process would use.
        def cancel() -> None:
            async def run() -> None:
                engine = create_async_engine(db.database_url)
                async with engine.begin() as connection:
                    await connection.execute(
                        text("UPDATE jobs SET status = 'cancelled' WHERE id = :id"),
                        {"id": payload["job_id"]},
                    )
                await engine.dispose()

            asyncio.run(run())

        thread = threading.Thread(target=cancel)
        thread.start()
        thread.join()
        return ExtractedDocument(
            source_version_id=source_object.source_version_id,
            media_type=source_object.media_type,
            language="en", blocks=[], warnings=[],
        )

    monkeypatch.setattr(task_module, "extract", cancel_during_extract)
    assert _worker(db, store_root, payload) == "cancelled"
    version, _audits, job, _source = _version_state(db, payload["job_id"])
    assert version == "uploaded"
    assert job == "cancelled"


def test_worker_unknown_version_fails_job(db, workspace, store_root) -> None:
    # Version deleted between enqueue and delivery (Task 17 purge path).
    bogus = {"job_id": str(uuid.uuid4()), "version_id": str(uuid.uuid4()),
             "source_id": str(uuid.uuid4())}

    async def enqueue() -> dict:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                job_id = await JobQueue().enqueue(
                    JobCommand(
                        type="source_extract",
                        payload={"version_id": bogus["version_id"], "source_id": bogus["source_id"]},
                        idempotency_key=f"source_extract:{bogus['version_id']}",
                    ),
                    connection,
                )
            return {**bogus, "job_id": str(job_id)}
        finally:
            await engine.dispose()

    payload = asyncio.run(enqueue())

    assert _worker(db, store_root, payload) == "missing_version"


def test_extracted_version_chains_the_index_job(
    db, workspace, store_root,
) -> None:
    """Extraction must dispatch indexing, or uploads stall half-processed."""

    accepted = _upload(db, store_root, workspace, data=_pdf("multilingual"), filename="chain.pdf")
    payload = _enqueue_and_payload(db, accepted, store_root)

    async def clean_outbox() -> None:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("DELETE FROM outbox WHERE topic = 'source_index'"))
        finally:
            await engine.dispose()

    asyncio.run(clean_outbox())
    assert _worker(db, store_root, payload) == "extracted"

    async def chained() -> list:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                return (
                    await connection.execute(
                        text("SELECT topic, payload FROM outbox WHERE topic = 'source_index'")
                    )
                ).all()
        finally:
            await engine.dispose()

    rows = asyncio.run(chained())
    assert len(rows) == 1
    topic, index_payload = rows[0]
    assert topic == "source_index"
    assert index_payload["version_id"] == accepted.source_version_id
    assert index_payload["source_id"] == accepted.source_id
    assert index_payload["job_id"]
