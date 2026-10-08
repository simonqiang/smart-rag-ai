"""Upload registration and safe original storage (Task 9).

Uploads are owner/admin-only and land as an immutable source version: the
original bytes go into the content-addressed ``ObjectStore`` (sha256 is the
address, so duplicates dedupe for free), and one transaction commits the
source, the version row, and both audit events. Rejections carry a machine
reason so routes can give actionable failures; rejected content never
creates rows.

# ponytail: type set is the Phase-3 MVP set (PDF/TXT/MD); broaden with Task 19 adapters
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text

from foundation.events import AuditEvent, EventWriter
from foundation.jobs import JobCommand, JobQueue
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import (
    AccessContext,
    ProtectedResource,
    authorize,
)
from source_catalog.catalog import CollectionNotFound

# ponytail: owner-confirmed 50 MB cap (spec §15); move to config when settings land
UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024

MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}

REJECTION_MESSAGES = {
    "over_limit": "file exceeds the 50 MB upload limit",
    "unsupported_type": "unsupported file type; supported: PDF, TXT, Markdown",
    "corrupt_pdf": "file is not a valid PDF (missing %PDF- header)",
    "encrypted_pdf": "password-protected PDFs are not supported; remove protection and retry",
}


class UploadRejected(Exception):
    """The upload cannot be accepted; ``reason`` is a stable machine code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(REJECTION_MESSAGES.get(reason, reason))


@dataclass(frozen=True)
class UploadAccepted:
    source_id: str
    source_version_id: str
    checksum: str
    size: int
    media_type: str
    duplicate: bool


def _media_type(filename: str) -> str | None:
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return MEDIA_TYPES.get(f".{suffix}")


def _validate(data: bytes, filename: str, max_bytes: int) -> str:
    if len(data) > max_bytes:
        raise UploadRejected("over_limit")
    media_type = _media_type(filename)
    if media_type is None:
        raise UploadRejected("unsupported_type")
    if media_type == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise UploadRejected("corrupt_pdf")
        if b"/Encrypt" in data:
            raise UploadRejected("encrypted_pdf")
    return media_type


async def register_upload(
    uow: UnitOfWork,
    writer: EventWriter,
    store: ObjectStore,
    *,
    context: AccessContext,
    collection_id: str,
    name: str,
    filename: str,
    data: bytes,
    max_bytes: int = UPLOAD_LIMIT_BYTES,
) -> UploadAccepted:
    authorize("source.create", ProtectedResource(context.workspace_id), context)
    media_type = _validate(data, filename, max_bytes)
    async with uow.transaction() as transaction:
        collection = (
            await transaction.execute(
                text("SELECT workspace_id FROM collections WHERE id = :id"),
                {"id": collection_id},
            )
        ).scalar_one_or_none()
        if collection is None or str(collection) != context.workspace_id:
            raise CollectionNotFound(collection_id)
        duplicate = (
            await transaction.execute(
                text(
                    "SELECT 1 FROM source_versions "
                    "WHERE workspace_id = :workspace_id AND object_sha256 = :sha256"
                ),
                {"workspace_id": context.workspace_id, "sha256": _sha256(data)},
            )
        ).first() is not None

    # Filesystem write outside the transaction: content-addressed objects are
    # idempotent, so a rolled-back commit leaves only a harmless orphan.
    address = store.put(data)

    source_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    async with uow.transaction() as transaction:
        await transaction.execute(
            text(
                "INSERT INTO sources (id, workspace_id, collection_id, name, "
                "state, created_by) VALUES (:id, :workspace_id, :collection_id, "
                ":name, 'active', :created_by)"
            ),
            {
                "id": source_id,
                "workspace_id": context.workspace_id,
                "collection_id": collection_id,
                "name": name,
                "created_by": context.user_id,
            },
        )
        await transaction.execute(
            text(
                "INSERT INTO source_versions (id, workspace_id, source_id, "
                "object_sha256, size_bytes, media_type, filename, state, created_by) "
                "VALUES (:id, :workspace_id, :source_id, :sha256, :size, "
                ":media_type, :filename, 'uploaded', :created_by)"
            ),
            {
                "id": version_id,
                "workspace_id": context.workspace_id,
                "source_id": source_id,
                "sha256": address,
                "size": len(data),
                "media_type": media_type,
                "filename": filename,
                "created_by": context.user_id,
            },
        )
        await writer.record(
            AuditEvent(
                type="source.created",
                actor=context.user_id,
                subject=source_id,
                metadata={"name": name, "collection_id": collection_id},
            ),
            transaction,
        )
        await writer.record(
            AuditEvent(
                type="source.uploaded",
                actor=context.user_id,
                subject=version_id,
                metadata={
                    "source_id": source_id,
                    "sha256": address,
                    "size": len(data),
                    "media_type": media_type,
                    "filename": filename,
                },
            ),
            transaction,
        )
        # Extraction dispatches only from this committed outbox row; the
        # idempotency key dedupes a redelivered upload transaction.
        await JobQueue().enqueue(
            JobCommand(
                type="source_extract",
                payload={"version_id": version_id, "source_id": source_id},
                idempotency_key=f"source_extract:{version_id}",
            ),
            transaction,
        )
    return UploadAccepted(
        source_id=source_id,
        source_version_id=version_id,
        checksum=address,
        size=len(data),
        media_type=media_type,
        duplicate=duplicate,
    )


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
