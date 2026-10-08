"""Shared fixtures for retrieval integration tests (live Compose PostgreSQL)."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_providers.fakes import FakeEmbeddingProvider
from foundation.config import ModelProfile, Settings
from foundation.events import EventWriter
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext, accessible_collection_ids
from identity_access.grants import set_user_collections
from identity_access.invitations import accept_invitation, issue_invitation_token
from identity_access.setup import create_first_owner
from ingestion.extraction import SourceObject, extract
from ingestion.uploads import register_upload
from knowledge_index.generations import activate_generation
from knowledge_index.indexer import index_version
from source_catalog.catalog import create_collection

TEST_PROFILE = ModelProfile(
    name="test", chat_model="fake-chat", embedding_model="fake-embed", embedding_dimensions=8,
)

EN_TEXT = "Smart RAG keeps every answer grounded in cited evidence from stored policy passages."
ZH_HANS_TEXT = "智能检索助手面向中小企业，提供本地知识问答服务，答案必须引用来源。"
ZH_HANT_TEXT = "員工學習國家語言與藝術，並且重視電話禮儀和個人操守。"
MS_TEXT = "Pengurusan data yang baik adalah penting untuk syarikat. Dasar keselamatan ini merangkumi langkah perlindungan maklumat."
MIXED_TEXT = "Quarterly review 季度審查報告 shows strong growth in every region."

RETRIEVAL_TABLES = (
    "index_chunks, index_generations, source_versions, sources, collections, "
    "invitations, sessions, users, workspaces, jobs, outbox"
)


def _run(db: Settings, coro_factory, store_root: Path | None = None) -> object:
    async def run() -> object:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        store = ObjectStore(store_root) if store_root else None
        try:
            return await coro_factory(engine, uow, store)
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load()


@pytest.fixture(scope="module")
def _migrated(settings: Settings):
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[3]
    command.upgrade(Config(str(root / "alembic.ini")), "head")


@pytest.fixture()
def db(_migrated, settings: Settings) -> Settings:
    async def reset() -> None:
        engine = create_async_engine(settings.database_url)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {RETRIEVAL_TABLES} CASCADE"))
            await connection.execute(text("TRUNCATE audit_events"))
        await engine.dispose()

    asyncio.run(reset())
    return settings


@pytest.fixture()
def store_root(db: Settings, tmp_path: Path) -> Path:
    return tmp_path / "store"


async def _index_source(
    engine, store: ObjectStore, context: AccessContext,
    *, collection_id: str, name: str, data: bytes, filename: str,
) -> dict:
    """Real upload → extract → index → activate pipeline with fake embeddings."""
    uow = UnitOfWork(engine)
    accepted = await register_upload(
        uow, EventWriter(), store,
        context=context, collection_id=collection_id, name=name,
        filename=filename, data=data,
    )
    document = extract(SourceObject(
        source_version_id=accepted.source_version_id,
        media_type=accepted.media_type,
        filename=filename,
        data=store.get(accepted.checksum),
    ))
    async with uow.transaction() as transaction:
        await transaction.execute(
            text("UPDATE source_versions SET state = 'extracted' WHERE id = :id"),
            {"id": accepted.source_version_id},
        )
    staged = await index_version(
        uow, FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
        workspace_id=context.workspace_id,
        source_version_id=accepted.source_version_id,
        document=document,
        profile=TEST_PROFILE,
    )
    generation = await activate_generation(
        uow, EventWriter(), staged.generation_id,
        workspace_id=context.workspace_id,
    )
    return {
        "source_id": accepted.source_id,
        "source_version_id": accepted.source_version_id,
        "generation_id": generation.id,
    }


@pytest.fixture()
def workspace(db: Settings, store_root: Path) -> dict:
    """Owner, member (granted Shared only), Shared and Restricted collections,
    and an indexed multilingual corpus."""
    texts = {
        "en": EN_TEXT, "zh-Hans": ZH_HANS_TEXT, "zh-Hant": ZH_HANT_TEXT,
        "ms": MS_TEXT, "mixed": MIXED_TEXT,
    }

    async def create() -> dict:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        owner_context = None
        try:
            owner = await create_first_owner(
                uow, EventWriter(), email="owner@example.com", password="owner-password-1"
            )
            owner_context = AccessContext(
                user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner",
            )
            shared = await create_collection(
                uow, EventWriter(), context=owner_context, name="Shared"
            )
            restricted = await create_collection(
                uow, EventWriter(), context=owner_context, name="Restricted"
            )
            invite = await issue_invitation_token(
                uow, EventWriter(), workspace_id=owner.workspace_id,
                email="member@example.com", role="member", invited_by=owner.user_id,
            )
            member = await accept_invitation(
                uow, EventWriter(), token=invite.token, password="member-password-1"
            )
            await set_user_collections(
                uow, EventWriter(),
                context=owner_context, target_user_id=member.user_id,
                collections=[shared.collection_id],
            )
            async def context_for(user_id: str, role: str) -> AccessContext:
                return AccessContext(
                    user_id=user_id, workspace_id=owner.workspace_id, role=role,
                    collection_ids=await accessible_collection_ids(
                        uow, user_id=user_id, workspace_id=owner.workspace_id
                    ),
                )

            member_context = await context_for(member.user_id, "member")
            owner_context = await context_for(owner.user_id, "owner")
            corpus = {}
            for language, body in texts.items():
                corpus[language] = await _index_source(
                    engine, ObjectStore(store_root), owner_context,
                    collection_id=shared.collection_id,
                    name=f"handbook-{language}",
                    data=body.encode("utf-8"),
                    filename=f"handbook-{language}.txt",
                )
            corpus["secret"] = await _index_source(
                engine, ObjectStore(store_root), owner_context,
                collection_id=restricted.collection_id,
                name="secret-doc",
                data=RESTRICTED_TEXT.encode("utf-8"),
                filename="secret.txt",
            )
        finally:
            await engine.dispose()
        return {
            "workspace_id": owner.workspace_id,
            "owner_id": owner.user_id,
            "member_id": member.user_id,
            "shared_collection_id": shared.collection_id,
            "restricted_collection_id": restricted.collection_id,
            "owner_context": owner_context,
            "member_context": member_context,
            "corpus": corpus,
            "texts": texts,
        }

    return asyncio.run(create())


RESTRICTED_TEXT = "Confidential bonus targets and unreleased financial results for executives."


def _owner(workspace: dict) -> AccessContext:
    return workspace["owner_context"]


def _member(workspace: dict) -> AccessContext:
    return workspace["member_context"]


def _chunk_id(db: Settings, fragment: str) -> str:
    async def run() -> str:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                return str((
                    await connection.execute(
                        text("SELECT id FROM index_chunks WHERE text LIKE :fragment"),
                        {"fragment": f"%{fragment}%"},
                    )
                ).scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(run())
