"""Task 14a: conversation store — bound history, ownership, deletion, resume."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext
from retrieval_answering.conversation import (
    HISTORY_MESSAGE_LIMIT,
    ConversationContext,
    ConversationMessage,
    ConversationNotFound,
    create_conversation,
    delete_conversation,
    delete_expired_conversations,
    find_completed_answer,
    get_conversation,
    list_conversations,
    load_context,
    record_exchange,
    rename_conversation,
)


def run_db[T](db: Settings, factory: Callable[[UnitOfWork], Awaitable[T]]) -> T:
    """Run one async block on its own engine, like the shared conftest helper."""

    async def run() -> T:
        engine = create_async_engine(db.database_url)
        try:
            return await factory(UnitOfWork(engine))
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_conversation_context_keeps_only_the_last_four_messages() -> None:
    context = ConversationContext()
    for index in range(6):
        context = context.appended("user" if index % 2 == 0 else "assistant", f"m{index}")
    assert len(context.messages) == HISTORY_MESSAGE_LIMIT == 4
    assert [message.content for message in context.messages] == ["m2", "m3", "m4", "m5"]


def test_conversation_context_clamps_constructed_history() -> None:
    flooded = ConversationContext(
        messages=tuple(
            ConversationMessage(role="user", content=f"m{index}") for index in range(9)
        )
    )
    assert [message.content for message in flooded.messages] == ["m5", "m6", "m7", "m8"]


def test_conversation_context_renders_role_labelled_prompt_lines() -> None:
    context = ConversationContext().appended("user", "你好").appended("assistant", "在")
    assert context.as_prompt_lines() == ["User: 你好", "Assistant: 在"]


def test_exchange_is_recorded_atomically_with_audit(db, workspace) -> None:
    owner: AccessContext = workspace["owner_context"]

    def flow(uow: UnitOfWork) -> Awaitable[str]:
        return create_conversation(uow, owner, title="Payroll policy")

    conversation_id = run_db(db, flow)

    def exchange(uow: UnitOfWork) -> Awaitable[None]:
        return record_exchange(
            uow, EventWriter(), context=owner, conversation_id=conversation_id,
            question="How much is the allowance?",
            answer_text="The allowance is 500 per month.",
            citations=[{"label": 1, "chunk_id": "c1", "quote": "allowance is 500"}],
            language="en", request_id="req-1", insufficient_evidence=False,
        )

    run_db(db, exchange)

    def verify(uow: UnitOfWork) -> Awaitable[tuple[list, list]]:
        async def read() -> tuple[list, list]:
            async with uow.transaction() as transaction:
                messages = (
                    await transaction.execute(
                        text("SELECT role, content, insufficient FROM conversation_messages "
                             "WHERE conversation_id = CAST(:id AS uuid) ORDER BY number"),
                        {"id": conversation_id},
                    )
                ).all()
                audits = (
                    await transaction.execute(
                        text("SELECT metadata FROM audit_events WHERE type = 'question.asked'"),
                    )
                ).scalars().all()
            return messages, audits

        return read()

    messages, audits = run_db(db, verify)
    assert [(role, content) for role, content, _flag in messages] == [
        ("user", "How much is the allowance?"),
        ("assistant", "The allowance is 500 per month."),
    ]
    assert messages[1][2] is False
    assert len(audits) == 1
    assert "How much" not in str(audits[0])  # audit metadata never carries question text


def test_list_orders_by_recency_and_scopes_to_the_user(db, workspace) -> None:
    owner: AccessContext = workspace["owner_context"]
    member: AccessContext = workspace["member_context"]

    def flow(uow: UnitOfWork) -> Awaitable[list[dict]]:
        async def create() -> list[dict]:
            await create_conversation(uow, owner, title="first")
            await create_conversation(uow, member, title="second")
            await create_conversation(uow, owner, title="third")
            return await list_conversations(uow, context=owner)

        return create()

    listed = run_db(db, flow)
    titles = [row["title"] for row in listed]
    assert "second" not in titles
    assert set(titles) == {"first", "third"}


def test_load_context_returns_bounded_history_for_the_owner(db, workspace) -> None:
    owner: AccessContext = workspace["owner_context"]

    def flow(uow: UnitOfWork) -> Awaitable[ConversationContext]:
        async def ask_four_times() -> ConversationContext:
            conversation_id = await create_conversation(uow, owner, title="history")
            for index in range(4):
                await record_exchange(
                    uow, EventWriter(), context=owner, conversation_id=conversation_id,
                    question=f"q{index}", answer_text=f"a{index}",
                    citations=[], language="en", request_id=f"req-{index}",
                    insufficient_evidence=False,
                )
            return await load_context(uow, context=owner, conversation_id=conversation_id)

        return ask_four_times()

    context = run_db(db, flow)
    contents = [message.content for message in context.messages]
    assert contents == ["q2", "a2", "q3", "a3"]


def test_conversation_access_is_private_to_its_user(db, workspace) -> None:
    owner: AccessContext = workspace["owner_context"]
    member: AccessContext = workspace["member_context"]

    conversation_id = run_db(db, lambda uow: create_conversation(uow, owner, title="private"))

    def denied(uow: UnitOfWork) -> Awaitable[None]:
        async def refuse() -> None:
            with pytest.raises(ConversationNotFound):
                await load_context(uow, context=member, conversation_id=conversation_id)
            with pytest.raises(ConversationNotFound):
                await get_conversation(uow, context=member, conversation_id=conversation_id)

        return refuse()

    run_db(db, denied)


def test_rename_and_delete_conversation(db, workspace) -> None:
    owner: AccessContext = workspace["owner_context"]

    def flow(uow: UnitOfWork) -> Awaitable[str]:
        async def rename_then_delete() -> str:
            conversation_id = await create_conversation(uow, owner, title="draft")
            await rename_conversation(uow, context=owner,
                                      conversation_id=conversation_id, title="renamed")
            listed = await list_conversations(uow, context=owner)
            assert listed[0]["title"] == "renamed"
            await delete_conversation(uow, context=owner, conversation_id=conversation_id)
            return conversation_id

        return rename_then_delete()

    conversation_id = run_db(db, flow)

    def gone(uow: UnitOfWork) -> Awaitable[None]:
        async def refuse_and_count() -> None:
            with pytest.raises(ConversationNotFound):
                await delete_conversation(uow, context=owner, conversation_id=conversation_id)
            async with uow.transaction() as transaction:
                count = (
                    await transaction.execute(
                        text("SELECT count(*) FROM conversation_messages "
                             "WHERE conversation_id = CAST(:id AS uuid)"),
                        {"id": conversation_id},
                    )
                ).scalar_one()
            assert count == 0

        return refuse_and_count()

    run_db(db, gone)


def test_delete_expired_conversations_keeps_recent_ones(db, workspace) -> None:
    owner: AccessContext = workspace["owner_context"]

    def seed(uow: UnitOfWork) -> Awaitable[None]:
        async def create() -> None:
            await create_conversation(uow, owner, title="old")
            await create_conversation(uow, owner, title="fresh")

        return create()

    run_db(db, seed)

    def backdate(uow: UnitOfWork) -> Awaitable[None]:
        async def age() -> None:
            async with uow.transaction() as transaction:
                await transaction.execute(
                    text("UPDATE conversations SET updated_at = now() - interval '40 days' "
                         "WHERE title = 'old'"),
                )

        return age()

    run_db(db, backdate)
    cutoff = datetime.now(UTC) - timedelta(days=30)
    deleted = run_db(db, lambda uow: delete_expired_conversations(uow, cutoff=cutoff))
    assert deleted == 1

    listed = run_db(db, lambda uow: list_conversations(uow, context=owner))
    assert [row["title"] for row in listed] == ["fresh"]


def test_find_completed_answer_supports_resume_and_refuses_others(db, workspace) -> None:
    owner: AccessContext = workspace["owner_context"]
    member: AccessContext = workspace["member_context"]

    def flow(uow: UnitOfWork) -> Awaitable[None]:
        async def record() -> None:
            conversation_id = await create_conversation(uow, owner, title="resume")
            await record_exchange(
                uow, EventWriter(), context=owner, conversation_id=conversation_id,
                question="q", answer_text="grounded answer",
                citations=[{"label": 1, "chunk_id": "c1", "quote": "quote"}],
                language="en", request_id="req-resume", insufficient_evidence=False,
            )

        return record()

    run_db(db, flow)

    found = run_db(
        db, lambda uow: find_completed_answer(uow, context=owner, request_id="req-resume")
    )
    assert found is not None
    assert found["content"] == "grounded answer"
    assert found["conversation_id"]
    assert found["insufficient"] is False
    assert run_db(
        db, lambda uow: find_completed_answer(uow, context=member, request_id="req-resume")
    ) is None
    assert run_db(
        db, lambda uow: find_completed_answer(uow, context=owner, request_id="req-unknown")
    ) is None
