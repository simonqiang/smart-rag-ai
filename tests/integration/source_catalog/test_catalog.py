"""Task 8a: source catalog — grant filtering, direct-ID denial, audit."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext, accessible_collection_ids
from identity_access.grants import set_user_collections
from identity_access.invitations import accept_invitation, issue_invitation_token
from identity_access.setup import create_first_owner
from source_catalog.catalog import (
    CollectionNotFound,
    SourceNotFound,
    create_collection,
    create_source,
    get_source,
    list_sources,
)

OWNER_EMAIL = "owner@example.com"
MEMBER_EMAIL = "member@example.com"


def _run(settings: Settings, coro_factory) -> object:
    async def run() -> object:
        engine = create_async_engine(settings.database_url)
        uow = UnitOfWork(engine)
        try:
            return await coro_factory(uow)
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.fixture()
def workspace(db: Settings) -> dict:
    async def create() -> dict:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            owner = await create_first_owner(uow, EventWriter(), email=OWNER_EMAIL, password="owner-password-1")
            invite = await issue_invitation_token(
                uow, EventWriter(), workspace_id=owner.workspace_id,
                email=MEMBER_EMAIL, role="member", invited_by=owner.user_id,
            )
            member = await accept_invitation(uow, EventWriter(), token=invite.token, password="member-password-1")
        finally:
            await engine.dispose()
        return {
            "workspace_id": owner.workspace_id,
            "owner_id": owner.user_id,
            "member_id": member.user_id,
        }

    return asyncio.run(create())


def _context(workspace: dict, key: str, role: str) -> AccessContext:
    return AccessContext(
        user_id=workspace[key], workspace_id=workspace["workspace_id"], role=role
    )


def test_create_collection_and_source_audits(db, workspace) -> None:
    async def create(uow) -> tuple:
        owner = _context(workspace, "owner_id", "owner")
        collection = await create_collection(uow, EventWriter(), context=owner, name="Policies")
        source = await create_source(
            uow, EventWriter(), context=owner,
            collection_id=collection.collection_id, name="Employee Handbook",
        )
        return collection, source

    collection, source = _run(db, create)
    assert source.source_id and source.collection_id == collection.collection_id
    assert source.state == "active"

    async def audits(uow) -> list:
        async with uow.transaction() as transaction:
            return (
                await transaction.execute(
                    text(
                        "SELECT type FROM audit_events "
                        "WHERE type IN ('collection.created', 'source.created') ORDER BY type"
                    )
                )
            ).scalars().all()

    assert _run(db, audits) == ["collection.created", "source.created"]


def test_create_collection_is_idempotent_on_name(db, workspace) -> None:
    async def create(uow) -> tuple[str, str]:
        owner = _context(workspace, "owner_id", "owner")
        first = await create_collection(uow, EventWriter(), context=owner, name="Policies")
        second = await create_collection(uow, EventWriter(), context=owner, name="Policies")
        return first.collection_id, second.collection_id

    first, second = _run(db, create)
    assert first == second

    async def count(uow) -> int:
        async with uow.transaction() as transaction:
            return (
                await transaction.execute(text("SELECT count(*) FROM collections"))
            ).scalar_one()

    assert _run(db, count) == 1


def test_create_source_refuses_foreign_or_unknown_collection(db, workspace) -> None:
    owner = _context(workspace, "owner_id", "owner")

    async def run(uow, collection_id: str) -> None:
        await create_source(
            uow, EventWriter(), context=owner,
            collection_id=collection_id, name="Whatever",
        )

    with pytest.raises(CollectionNotFound):
        _run(db, lambda uow: run(uow, str(uuid.uuid4())))

    async def seed_foreign(uow) -> str:
        foreign_ws = str(uuid.uuid4())
        collection_id = str(uuid.uuid4())
        async with uow.transaction() as transaction:
            await transaction.execute(
                text("INSERT INTO workspaces (id, name) VALUES (:id, 'Foreign')"),
                {"id": foreign_ws},
            )
            await transaction.execute(
                text(
                    "INSERT INTO collections (id, workspace_id, name) "
                    "VALUES (:id, :workspace_id, 'Foreign collection')"
                ),
                {"id": collection_id, "workspace_id": foreign_ws},
            )
        return collection_id

    async def run_foreign(uow) -> None:
        foreign = await seed_foreign(uow)
        await run(uow, foreign)

    with pytest.raises(CollectionNotFound):
        _run(db, run_foreign)


def test_owner_sees_all_member_sees_only_granted(db, workspace) -> None:
    async def seed(uow) -> tuple[str, str, str, str]:
        owner = _context(workspace, "owner_id", "owner")
        member = _context(workspace, "member_id", "member")
        shared = await create_collection(uow, EventWriter(), context=owner, name="Shared")
        private = await create_collection(uow, EventWriter(), context=owner, name="Private")
        shared_source = await create_source(
            uow, EventWriter(), context=owner,
            collection_id=shared.collection_id, name="Handbook",
        )
        await create_source(
            uow, EventWriter(), context=owner,
            collection_id=private.collection_id, name="Board notes",
        )
        await set_user_collections(
            uow, EventWriter(), context=owner,
            target_user_id=workspace["member_id"], collections=[shared.collection_id],
        )
        return shared.collection_id, private.collection_id, shared_source.source_id, member

    shared_id, _, _, member = _run(db, seed)

    async def sources_for(uow, context: AccessContext) -> list:
        return await list_sources(uow, context=context)

    owner_view = _run(db, lambda uow: sources_for(uow, _context(workspace, "owner_id", "owner")))
    member_view = _run(db, lambda uow: sources_for(uow, member))
    assert [source.name for source in owner_view] == ["Handbook", "Board notes"]
    assert [source.name for source in member_view] == ["Handbook"]
    assert member_view[0].collection_id == shared_id


def test_get_source_direct_id_rules(db, workspace) -> None:
    async def seed(uow) -> tuple[str, str]:
        owner = _context(workspace, "owner_id", "owner")
        granted = await create_collection(uow, EventWriter(), context=owner, name="Shared")
        other = await create_collection(uow, EventWriter(), context=owner, name="Other")
        source = await create_source(
            uow, EventWriter(), context=owner,
            collection_id=other.collection_id, name="Handbook",
        )
        # Member can see "Shared" but the source lives in "Other".
        await set_user_collections(
            uow, EventWriter(), context=owner,
            target_user_id=workspace["member_id"], collections=[granted.collection_id],
        )
        return source.source_id, other.collection_id

    source_id, _ = _run(db, seed)

    async def get(uow, context: AccessContext, source: str) -> object:
        return await get_source(uow, context=context, source_id=source)

    owner = _context(workspace, "owner_id", "owner")
    member = _context(workspace, "member_id", "member")

    found = _run(db, lambda uow: get(uow, owner, source_id))
    assert found.source_id == source_id

    # Member's grants do not cover the source's collection.
    with pytest.raises(SourceNotFound):
        _run(db, lambda uow: get(uow, member, source_id))

    with pytest.raises(SourceNotFound):
        _run(db, lambda uow: get(uow, member, str(uuid.uuid4())))

    async def foreign(uow) -> None:
        from identity_access.passwords import hash_password

        foreign_ws = str(uuid.uuid4())
        foreign_user = str(uuid.uuid4())
        foreign_collection = str(uuid.uuid4())
        foreign_source = str(uuid.uuid4())
        async with uow.transaction() as transaction:
            await transaction.execute(
                text("INSERT INTO workspaces (id, name) VALUES (:id, 'Foreign')"),
                {"id": foreign_ws},
            )
            await transaction.execute(
                text(
                    "INSERT INTO users (id, workspace_id, email, password_hash, role) "
                    "VALUES (:id, :workspace_id, 'f@example.com', :hash, 'owner')"
                ),
                {"id": foreign_user, "workspace_id": foreign_ws, "hash": hash_password("f-pass-1")},
            )
            await transaction.execute(
                text(
                    "INSERT INTO collections (id, workspace_id, name) "
                    "VALUES (:id, :workspace_id, 'Foreign collection')"
                ),
                {"id": foreign_collection, "workspace_id": foreign_ws},
            )
            await transaction.execute(
                text(
                    "INSERT INTO sources (id, workspace_id, collection_id, name, created_by) "
                    "VALUES (:id, :workspace_id, :collection_id, 'Foreign source', :created_by)"
                ),
                {
                    "id": foreign_source,
                    "workspace_id": foreign_ws,
                    "collection_id": foreign_collection,
                    "created_by": foreign_user,
                },
            )
        await get(uow, member, foreign_source)

    with pytest.raises(SourceNotFound):
        _run(db, foreign)


def test_accessible_collection_ids_enumerates_workspace_collections(db, workspace) -> None:
    async def seed(uow) -> None:
        owner = _context(workspace, "owner_id", "owner")
        first = await create_collection(uow, EventWriter(), context=owner, name="Alpha")
        second = await create_collection(uow, EventWriter(), context=owner, name="Beta")
        await set_user_collections(
            uow, EventWriter(), context=owner,
            target_user_id=workspace["member_id"], collections=[first.collection_id],
        )
        assert second.collection_id  # both created

    _run(db, seed)

    async def access(uow, *, user_id: str) -> list:
        return await accessible_collection_ids(
            uow, user_id=user_id, workspace_id=workspace["workspace_id"]
        )

    owner_ids = _run(db, lambda uow: access(uow, user_id=workspace["owner_id"]))
    member_ids = _run(db, lambda uow: access(uow, user_id=workspace["member_id"]))
    assert len(owner_ids) == 2
    assert member_ids == owner_ids[:1]


def test_member_without_grants_lists_nothing(db, workspace) -> None:
    async def seed(uow) -> None:
        owner = _context(workspace, "owner_id", "owner")
        collection = await create_collection(uow, EventWriter(), context=owner, name="Private")
        await create_source(
            uow, EventWriter(), context=owner,
            collection_id=collection.collection_id, name="Board notes",
        )

    _run(db, seed)
    member = _context(workspace, "member_id", "member")
    assert _run(db, lambda uow: list_sources(uow, context=member)) == []
