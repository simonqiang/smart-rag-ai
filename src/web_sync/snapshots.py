"""Website snapshot staging (Task 20c).

``build_snapshot`` renders a crawl as the canonical content-addressed JSON
bundle (see ``ingestion.parsers.snapshot``); ``stage_snapshot`` registers it
through the same immutable source-version contract as an upload — source row
when new, version in ``uploaded``, transactional audits, and a
``source_extract`` dispatch that commits with the version row. The object is
content-addressed, so an unchanged re-crawl simply points a new version at
the already-stored bytes; the still-active short-circuit is Task 21.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import text

from foundation.events import AuditEvent, EventWriter
from foundation.jobs import JobCommand, JobQueue
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext, ProtectedResource, authorize
from ingestion.parsers.snapshot import SNAPSHOT_MEDIA_TYPE, SnapshotPage, encode_bundle
from source_catalog.catalog import CollectionNotFound
from web_sync.crawler import CrawlResult


@dataclass(frozen=True)
class SnapshotBundle:
    pages: list[SnapshotPage]
    data: bytes


@dataclass(frozen=True)
class SnapshotStaged:
    source_id: str
    source_version_id: str
    checksum: str
    page_count: int
    filename: str


def build_snapshot(result: CrawlResult) -> SnapshotBundle:
    """Render crawled pages as the bundle; crawl order becomes page order."""
    pages = [
        SnapshotPage(url=page.url, blocks=[block.text for block in page.blocks])
        for page in result.pages
    ]
    return SnapshotBundle(pages=pages, data=encode_bundle(pages))


def _filename(start_url: str) -> str:
    return f"{urlsplit(start_url).hostname or 'site'}.snapshot.json"


async def stage_snapshot(
    uow: UnitOfWork,
    writer: EventWriter,
    store: ObjectStore,
    *,
    context: AccessContext,
    collection_id: str,
    name: str,
    start_url: str,
    bundle: SnapshotBundle,
) -> SnapshotStaged:
    authorize("source.create", ProtectedResource(context.workspace_id), context)
    filename = _filename(start_url)
    # Content-addressed storage: an unchanged re-crawl reuses the bytes.
    address = store.put(bundle.data)
    source_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    async with uow.transaction() as transaction:
        collection = (
            await transaction.execute(
                text("SELECT workspace_id FROM collections WHERE id = :id"),
                {"id": collection_id},
            )
        ).scalar_one_or_none()
        if collection is None or str(collection) != context.workspace_id:
            raise CollectionNotFound(collection_id)
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
                "size": len(bundle.data),
                "media_type": SNAPSHOT_MEDIA_TYPE,
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
                    "size": len(bundle.data),
                    "media_type": SNAPSHOT_MEDIA_TYPE,
                    "filename": filename,
                    "start_url": start_url,
                    "pages": len(bundle.pages),
                },
            ),
            transaction,
        )
        # Extraction dispatches only from this committed outbox row; the
        # idempotency key dedupes a redelivered stage transaction.
        await JobQueue().enqueue(
            JobCommand(
                type="source_extract",
                payload={"version_id": version_id, "source_id": source_id},
                idempotency_key=f"source_extract:{version_id}",
            ),
            transaction,
        )
    return SnapshotStaged(
        source_id=source_id,
        source_version_id=version_id,
        checksum=address,
        page_count=len(bundle.pages),
        filename=filename,
    )
