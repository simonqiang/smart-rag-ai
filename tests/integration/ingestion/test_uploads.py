"""Task 9a: upload registration — validation, checksums, duplicates, denials."""

import asyncio
import hashlib
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext, AccessDenied
from ingestion.uploads import (
    UploadRejected,
    register_upload,
)
from source_catalog.catalog import CollectionNotFound

VALID_PDF = b"%PDF-1.7 fake pdf body for testing"
ENCRYPTED_PDF = b"%PDF-1.7 /Encrypt 4 0 R fake encrypted body"


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
        user_id=workspace["owner_id"],
        workspace_id=workspace["workspace_id"],
        role="owner",
    )


def _upload(db: Settings, store_root: Path, workspace: dict, *, data: bytes,
            filename: str = "handbook.pdf", collection_id: str | None = None,
            context: AccessContext | None = None,
            max_bytes: int = 50 * 1024 * 1024) -> object:
    async def run(uow, store: ObjectStore) -> object:
        return await register_upload(
            uow, EventWriter(), store,
            context=context or _owner_context(workspace),
            collection_id=collection_id or workspace["collection_id"],
            name="Employee Handbook",
            filename=filename,
            data=data,
            max_bytes=max_bytes,
        )

    return _run(db, run, store_root)


def test_valid_pdf_creates_source_version_object_and_audits(db, workspace, store_root) -> None:
    accepted = _upload(db, store_root, workspace, data=VALID_PDF)
    checksum = hashlib.sha256(VALID_PDF).hexdigest()

    assert accepted.source_version_id
    assert accepted.source_id
    assert accepted.checksum == checksum
    assert accepted.size == len(VALID_PDF)
    assert accepted.duplicate is False

    async def verify(uow, store: ObjectStore) -> tuple:
        async with uow.transaction() as transaction:
            version = (
                await transaction.execute(text("SELECT state, media_type FROM source_versions"))
            ).mappings().one()
            source = (
                await transaction.execute(text("SELECT state FROM sources"))
            ).mappings().one()
            audits = (
                await transaction.execute(
                    text("SELECT type FROM audit_events WHERE type LIKE 'source.%' ORDER BY type")
                )
            ).scalars().all()
        stored = store.get(accepted.checksum)
        manifest = store.manifest(accepted.checksum)
        return dict(version), dict(source), list(audits), stored, manifest

    version, source, audits, stored, manifest = _run(db, verify, store_root)
    assert version == {"state": "uploaded", "media_type": "application/pdf"}
    assert source == {"state": "active"}
    assert audits == ["source.created", "source.uploaded"]
    assert stored == VALID_PDF
    assert manifest["sha256"] == checksum
    assert manifest["size"] == len(VALID_PDF)


def test_duplicate_checksum_warns_and_reuses_object(db, workspace, store_root) -> None:
    first = _upload(db, store_root, workspace, data=VALID_PDF)
    second = _upload(
        db, store_root, workspace, data=VALID_PDF, filename="handbook-copy.pdf"
    )

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.checksum == first.checksum

    async def objects(db: Settings) -> int:
        return len(list((store_root / "objects").iterdir()))

    assert asyncio.run(objects(db)) == 1


def test_oversized_upload_refused_with_reason(db, workspace, store_root) -> None:
    with pytest.raises(UploadRejected) as excinfo:
        _upload(db, store_root, workspace, data=b"x" * 11, max_bytes=10)

    assert excinfo.value.reason == "over_limit"

    async def counts(uow, _store) -> tuple:
        async with uow.transaction() as transaction:
            versions = (
                await transaction.execute(text("SELECT count(*) FROM source_versions"))
            ).scalar_one()
            sources = (
                await transaction.execute(text("SELECT count(*) FROM sources"))
            ).scalar_one()
        return versions, sources

    versions, sources = _run(db, counts, store_root)
    assert (versions, sources) == (0, 0)


def test_corrupt_pdf_refused_with_reason(db, workspace, store_root) -> None:
    with pytest.raises(UploadRejected) as excinfo:
        _upload(db, store_root, workspace, data=b"not a pdf at all", filename="broken.pdf")

    assert excinfo.value.reason == "corrupt_pdf"


def test_encrypted_pdf_refused_with_reason(db, workspace, store_root) -> None:
    with pytest.raises(UploadRejected) as excinfo:
        _upload(db, store_root, workspace, data=ENCRYPTED_PDF, filename="locked.pdf")

    assert excinfo.value.reason == "encrypted_pdf"


def test_unsupported_type_refused_with_reason(db, workspace, store_root) -> None:
    with pytest.raises(UploadRejected) as excinfo:
        _upload(db, store_root, workspace, data=b"MZ binary", filename="tool.exe")

    assert excinfo.value.reason == "unsupported_type"


def test_txt_and_md_accepted_with_media_types(db, workspace, store_root) -> None:
    txt = _upload(db, store_root, workspace, data=b"hello", filename="notes.txt")
    md = _upload(db, store_root, workspace, data=b"# hi", filename="readme.md")

    assert txt.media_type == "text/plain"
    assert md.media_type == "text/markdown"


def test_member_cannot_upload(db, workspace, store_root) -> None:
    # The role gate fires before any target lookup, so no member row is needed.
    member = AccessContext(
        user_id=str(uuid.uuid4()),
        workspace_id=workspace["workspace_id"],
        role="member",
    )
    with pytest.raises(AccessDenied):
        _upload(db, store_root, workspace, data=VALID_PDF, context=member)


def test_foreign_collection_refused(db, workspace, store_root) -> None:
    with pytest.raises(CollectionNotFound):
        _upload(
            db, store_root, workspace, data=VALID_PDF, collection_id=str(uuid.uuid4())
        )
