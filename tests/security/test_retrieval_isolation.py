"""Task 13: retrieval isolation — zero leakage across grants, versions, sources.

Every proof runs the real hybrid retriever against a live corpus and asserts
the unauthorized or stale row never appears in the ranking (it must never
leave PostgreSQL, not merely be filtered afterwards).
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_providers.fakes import FakeEmbeddingProvider
from foundation.config import ModelProfile, Settings
from foundation.events import EventWriter
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext, accessible_collection_ids
from identity_access.grants import disable_user, set_user_collections
from identity_access.invitations import accept_invitation, issue_invitation_token
from identity_access.setup import create_first_owner
from ingestion.extraction import SourceObject, extract
from ingestion.uploads import register_upload
from knowledge_index.generations import activate_generation
from knowledge_index.indexer import index_version
from retrieval_answering.retrieval import retrieve
from source_catalog.catalog import create_collection

TEST_PROFILE = ModelProfile(
    name="test", chat_model="fake-chat", embedding_model="fake-embed", embedding_dimensions=8,
)
SHARED_TEXT = "Public onboarding steps for every employee joining the support team."
SECRET_TEXT = "Executive redundancy payouts negotiated behind closed doors."

RETRIEVAL_TABLES = (
    "index_chunks, index_generations, source_versions, sources, collections, "
    "invitations, sessions, users, workspaces, jobs, outbox"
)


def _run(db: Settings, coro_factory, store_root) -> object:
    async def run() -> object:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            return await coro_factory(engine, uow, ObjectStore(store_root))
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load()


@pytest.fixture(scope="module")
def _migrated(settings: Settings):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
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
def store_root(db: Settings, tmp_path) -> object:
    return tmp_path / "store"


async def _index_source(engine, store, context: AccessContext, *, collection_id: str,
                        name: str, data: bytes) -> dict:
    uow = UnitOfWork(engine)
    accepted = await register_upload(
        uow, EventWriter(), store, context=context, collection_id=collection_id,
        name=name, filename=f"{name}.txt", data=data,
    )
    document = extract(SourceObject(
        source_version_id=accepted.source_version_id,
        media_type=accepted.media_type, filename=f"{name}.txt",
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
        document=document, profile=TEST_PROFILE,
    )
    await activate_generation(
        uow, EventWriter(), staged.generation_id, workspace_id=context.workspace_id,
    )
    return accepted.__dict__ | {"source_version_id": accepted.source_version_id}


@pytest.fixture()
def world(db: Settings, store_root) -> dict:
    """Workspace A (Shared+Restricted, member granted Shared) and workspace B
    holding an identical copy of the shared text."""

    async def create() -> dict:
        engine = create_async_engine(db.database_url)

        async def build_workspace(email: str, with_member: bool) -> dict:
            uow = UnitOfWork(engine)
            owner = await create_first_owner(
                uow, EventWriter(), email=email, password="owner-password-1"
            )
            owner_context = AccessContext(
                user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner"
            )
            shared = await create_collection(
                uow, EventWriter(), context=owner_context, name="Shared"
            )
            restricted = await create_collection(
                uow, EventWriter(), context=owner_context, name="Restricted"
            )
            state = {
                "workspace_id": owner.workspace_id, "owner_id": owner.user_id,
                "shared_collection_id": shared.collection_id,
                "restricted_collection_id": restricted.collection_id,
            }
            state["shared"] = await _index_source(
                engine, ObjectStore(store_root), owner_context,
                collection_id=shared.collection_id, name="onboarding",
                data=SHARED_TEXT.encode(),
            )
            state["secret"] = await _index_source(
                engine, ObjectStore(store_root), owner_context,
                collection_id=restricted.collection_id, name="redundancy",
                data=SECRET_TEXT.encode(),
            )
            if with_member:
                invite = await issue_invitation_token(
                    uow, EventWriter(), workspace_id=owner.workspace_id,
                    email="member@example.com", role="member", invited_by=owner.user_id,
                )
                member = await accept_invitation(
                    uow, EventWriter(), token=invite.token, password="member-password-1"
                )
                await set_user_collections(
                    uow, EventWriter(), context=owner_context,
                    target_user_id=member.user_id, collections=[shared.collection_id],
                )
                state["member_id"] = member.user_id
            return state

        workspace_a = await build_workspace("owner-a@example.com", with_member=True)

        # A second workspace: created directly (first-owner exclusivity is
        # global), with a synthetic admin context — retrieval never consults
        # the user row, only the workspace-scoped data.
        workspace_b_id = str(uuid.uuid4())
        synthetic_owner = str(uuid.uuid4())
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO workspaces (id, name) VALUES (:id, :name)"),
                {"id": workspace_b_id, "name": "Workspace B"},
            )
            await connection.execute(
                text(
                    "INSERT INTO users (id, workspace_id, email, password_hash, role) "
                    "VALUES (:id, :ws, 'owner-b@example.com', 'not-a-login', 'owner')"
                ),
                {"id": synthetic_owner, "ws": workspace_b_id},
            )
        workspace_b = {"workspace_id": workspace_b_id}
        uow = UnitOfWork(engine)
        synthetic = AccessContext(
            user_id=synthetic_owner, workspace_id=workspace_b_id, role="owner"
        )
        shared_b = await create_collection(
            uow, EventWriter(), context=synthetic, name="Shared"
        )
        workspace_b["shared"] = await _index_source(
            engine, ObjectStore(store_root), synthetic,
            collection_id=shared_b.collection_id, name="onboarding",
            data=SHARED_TEXT.encode(),
        )
        workspace_b["secret"] = {"source_id": str(uuid.uuid4())}

        async def context_of(state: dict, user_id: str, role: str) -> AccessContext:
            return AccessContext(
                user_id=user_id, workspace_id=state["workspace_id"], role=role,
                collection_ids=await accessible_collection_ids(
                    UnitOfWork(engine), user_id=user_id,
                    workspace_id=state["workspace_id"],
                ),
            )

        workspace_a["owner_context"] = await context_of(
            workspace_a, workspace_a["owner_id"], "owner"
        )
        workspace_a["member_context"] = await context_of(
            workspace_a, workspace_a["member_id"], "member"
        )
        workspace_b["owner_context"] = AccessContext(
            user_id=synthetic_owner, workspace_id=workspace_b_id, role="owner",
            collection_ids=[shared_b.collection_id],
        )
        await engine.dispose()
        return workspace_a | {"workspace_b": workspace_b}

    return asyncio.run(create())


def _retrieve(db, context: AccessContext, query: str, store_root, **kwargs) -> list:
    async def run(engine, uow, _store) -> list:
        ranked = await retrieve(
            uow, FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
            context=context, query=query, profile=TEST_PROFILE, **kwargs,
        )
        return [item.chunk_id for item in ranked.items]

    return _run(db, run, store_root)


def _chunk_of(db: Settings, source: dict) -> str:
    async def run() -> str:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                return str((
                    await connection.execute(
                        text("SELECT id FROM index_chunks WHERE source_id = :id"),
                        {"id": source["source_id"]},
                    )
                ).scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_member_with_grant_retrieves_shared_but_never_restricted(
    db, world, store_root,
) -> None:
    ranked = _retrieve(db, world["member_context"], SHARED_TEXT, store_root)

    assert _chunk_of(db, world["shared"]) in ranked
    assert _chunk_of(db, world["secret"]) not in ranked

    # Even the exact restricted text as the query leaks nothing.
    secret_query = _retrieve(db, world["member_context"], SECRET_TEXT, store_root)
    assert _chunk_of(db, world["secret"]) not in secret_query


def test_owner_sees_both_collections(db, world, store_root) -> None:
    ranked = _retrieve(db, world["owner_context"], SHARED_TEXT, store_root)

    assert _chunk_of(db, world["shared"]) in ranked
    assert _chunk_of(db, world["secret"]) in ranked


def test_revoked_grant_excludes_content_immediately(db, world, store_root) -> None:
    async def revoke(engine, _uow, _store) -> None:
        await set_user_collections(
            UnitOfWork(engine), EventWriter(),
            context=world["owner_context"], target_user_id=world["member_id"],
            collections=[],
        )
        world["member_context"] = AccessContext(
            user_id=world["member_id"], workspace_id=world["workspace_id"],
            role="member",
            collection_ids=await accessible_collection_ids(
                UnitOfWork(engine), user_id=world["member_id"],
                workspace_id=world["workspace_id"],
            ),
        )

    _run(db, revoke, store_root)
    assert _retrieve(db, world["member_context"], SHARED_TEXT, store_root) == []


def test_disabled_user_retrieves_nothing(db, world, store_root) -> None:
    async def disable(engine, _uow, _store) -> None:
        await disable_user(
            UnitOfWork(engine), EventWriter(),
            context=world["owner_context"], target_user_id=world["member_id"],
        )
        world["member_context"] = AccessContext(
            user_id=world["member_id"], workspace_id=world["workspace_id"],
            role="member",
            collection_ids=await accessible_collection_ids(
                UnitOfWork(engine), user_id=world["member_id"],
                workspace_id=world["workspace_id"],
            ),
        )

    _run(db, disable, store_root)
    assert _retrieve(db, world["member_context"], SHARED_TEXT, store_root) == []


def test_cross_workspace_copy_never_leaks(db, world, store_root) -> None:
    # Workspace B holds byte-identical content; workspace A's contexts must
    # never surface it and vice versa.
    for context in (world["owner_context"], world["member_context"]):
        ranked = _retrieve(db, context, SHARED_TEXT, store_root)
        assert _chunk_of(db, world["workspace_b"]["shared"]) not in ranked

    # Workspace B's owner sees only B's copy; B has no restricted content.
    b_ranked = _retrieve(db, world["workspace_b"]["owner_context"], SHARED_TEXT, store_root)
    assert set(b_ranked) == {_chunk_of(db, world["workspace_b"]["shared"])}


def test_direct_source_id_of_restricted_collection_yields_empty_for_member(
    db, world, store_root,
) -> None:
    ranked = _retrieve(
        db, world["member_context"], SECRET_TEXT, store_root,
        source_id=world["secret"]["source_id"],
    )
    assert ranked == []


def test_direct_source_id_of_granted_collection_scopes_results(
    db, world, store_root,
) -> None:
    ranked = _retrieve(
        db, world["member_context"], SHARED_TEXT, store_root,
        source_id=world["shared"]["source_id"],
    )
    assert ranked == [_chunk_of(db, world["shared"])]


def test_retired_generation_is_excluded_after_replacement(db, world, store_root) -> None:
    stale_chunk = _chunk_of(db, world["shared"])

    async def replace(engine, uow, store) -> None:
        context = world["owner_context"]
        version_id = world["shared"]["source_version_id"]
        async with uow.transaction() as transaction:
            sha256 = (
                await transaction.execute(
                    text("SELECT object_sha256 FROM source_versions WHERE id = :id"),
                    {"id": version_id},
                )
            ).scalar_one()
            await transaction.execute(
                text("UPDATE source_versions SET state = 'extracted' WHERE id = :id"),
                {"id": version_id},
            )
        document = extract(SourceObject(
            source_version_id=version_id, media_type="text/plain",
            filename="onboarding.txt",
            data=store.get(str(sha256)),
        ))
        staged = await index_version(
            uow, FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
            workspace_id=context.workspace_id, source_version_id=version_id,
            document=document, profile=TEST_PROFILE,
        )
        await activate_generation(
            uow, EventWriter(), staged.generation_id,
            workspace_id=context.workspace_id,
        )

    _run(db, replace, store_root)

    for context in (world["owner_context"], world["member_context"]):
        ranked = _retrieve(db, context, SHARED_TEXT, store_root)
        assert ranked  # the replacement's chunks are retrievable
        assert stale_chunk not in ranked


def test_archived_source_is_excluded_from_retrieval(db, world, store_root) -> None:
    shared_chunk = _chunk_of(db, world["shared"])

    async def archive(engine, _uow, _store) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE sources SET state = 'archived' WHERE id = :id"),
                {"id": world["shared"]["source_id"]},
            )

    _run(db, archive, store_root)

    for context in (world["owner_context"], world["member_context"]):
        assert shared_chunk not in _retrieve(db, context, SHARED_TEXT, store_root)


def test_unknown_user_retrieves_nothing(db, world, store_root) -> None:
    stranger = AccessContext(
        user_id=str(uuid.uuid4()), workspace_id=world["workspace_id"], role="member",
        collection_ids=[],
    )
    assert _retrieve(db, stranger, SHARED_TEXT, store_root) == []
