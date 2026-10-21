"""Task 25: answer feedback and privacy-safe analytics aggregates.

Feedback is an ownership-checked upsert on a stored answer (duplicates are
last-write-wins). Aggregates are counts only — never question or answer
text — and classify unanswered asks as insufficient-evidence exchanges plus
messageless conversations (failed or cancelled first asks; completion-gated
persistence leaves no other trace, ``ponytail:`` outcome rows on the error
path if operators ever need finer classes).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext
from identity_access.setup import create_first_owner
from operations.feedback import (
    FeedbackNotFound,
    analytics_summary,
    record_feedback,
)
from retrieval_answering.conversation import create_conversation, record_exchange

T = TypeVar("T")

OWNER = {"email": "owner@example.com", "password": "owner-password-1"}

FEEDBACK_TABLES = (
    "answer_feedback, conversation_messages, conversations, index_chunks, "
    "index_generations, source_versions, sources, collections, invitations, "
    "sessions, users, workspaces, jobs, outbox, managed_backups"
)


@pytest.fixture(scope="module", autouse=True)
def _migrated():
    root = Path(__file__).resolve().parents[3]
    command.upgrade(Config(str(root / "alembic.ini")), "head")


@pytest.fixture()
def workspace():
    async def create() -> dict:
        engine = create_async_engine(Settings.load().database_url)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {FEEDBACK_TABLES} CASCADE"))
            await connection.execute(text("TRUNCATE audit_events"))
        uow = UnitOfWork(engine)
        try:
            owner = await create_first_owner(uow, EventWriter(), **OWNER)
            context = AccessContext(
                user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner",
            )
            answered = await create_conversation(uow, context, title="policy thread")
            await record_exchange(
                uow, EventWriter(), context=context, conversation_id=answered,
                question="what is the refund policy",
                answer_text="Refunds take 14 days [1:refund window].",
                citations=[{"label": 1, "chunk_id": "c1", "quote": "refund window"}],
                language="en", request_id="req-answered", insufficient_evidence=False,
            )
            bypassed = await create_conversation(uow, context, title="thin corpus")
            await record_exchange(
                uow, EventWriter(), context=context, conversation_id=bypassed,
                question="what about mars relocation",
                answer_text="I don't have enough indexed evidence to answer this question.",
                citations=[], language="en", request_id="req-insufficient",
                insufficient_evidence=True,
            )
            await create_conversation(uow, context, title="abandoned ask")
        finally:
            await engine.dispose()
        return {"context": context}

    return asyncio.run(create())


def db_run[T](factory: Callable[[UnitOfWork], Awaitable[T]]) -> T:
    """One engine per event loop, like the conversation test helper."""

    async def run() -> T:
        engine = create_async_engine(Settings.load().database_url)
        try:
            return await factory(UnitOfWork(engine))
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_feedback_records_helpful_and_unhelpful(workspace) -> None:
    context: AccessContext = workspace["context"]

    async def seed(uow: UnitOfWork) -> None:
        await record_feedback(uow, context=context, request_id="req-answered",
                              helpful=True)
        await record_feedback(uow, context=context, request_id="req-insufficient",
                              helpful=False)

    db_run(seed)
    summary = db_run(lambda uow: analytics_summary(uow, context=context))
    assert summary["helpful"] == 1
    assert summary["unhelpful"] == 1


def test_duplicate_feedback_is_last_write_wins(workspace) -> None:
    context: AccessContext = workspace["context"]

    async def twice(uow: UnitOfWork) -> None:
        await record_feedback(uow, context=context, request_id="req-answered",
                              helpful=True)
        await record_feedback(uow, context=context, request_id="req-answered",
                              helpful=False)

    db_run(twice)

    async def count(uow: UnitOfWork) -> tuple[int, int]:
        async with uow.transaction() as tx:
            row = (await tx.execute(
                text("SELECT count(*), count(*) FILTER (WHERE helpful) "
                     "FROM answer_feedback"),
            )).one()
        return row[0], row[1]

    assert db_run(count) == (1, 0)


def test_feedback_refuses_unknown_answers(workspace) -> None:
    context: AccessContext = workspace["context"]

    async def go(uow: UnitOfWork) -> None:
        await record_feedback(uow, context=context, request_id="no-such-request",
                              helpful=True)

    with pytest.raises(FeedbackNotFound):
        db_run(go)


def test_summary_classifies_unanswered_and_insufficient(workspace) -> None:
    context: AccessContext = workspace["context"]
    summary = db_run(lambda uow: analytics_summary(uow, context=context))
    assert summary["exchanges"] == 2
    assert summary["answered"] == 1
    assert summary["insufficient_evidence"] == 1
    assert summary["unanswered_conversations"] == 1


def test_summary_stays_count_only_after_deletion_redaction(workspace) -> None:
    """A redacted answer (Task 17 deletion notice) still counts, never leaks."""
    context: AccessContext = workspace["context"]

    async def redact(uow: UnitOfWork) -> None:
        async with uow.transaction() as tx:
            await tx.execute(
                text("UPDATE conversation_messages SET content = :notice "
                     "WHERE request_id = 'req-answered'"),
                {"notice": "[deleted source removed this answer]"},
            )

    db_run(redact)
    db_run(lambda uow: record_feedback(uow, context=context,
                                       request_id="req-answered", helpful=False))
    summary = db_run(lambda uow: analytics_summary(uow, context=context))
    assert summary["unhelpful"] == 1
    assert summary["exchanges"] == 2
    flat = str(summary)
    assert "refund policy" not in flat
    assert "mars relocation" not in flat
    assert "[deleted source" not in flat


def test_empty_workspace_summary_is_all_zero(workspace) -> None:
    context: AccessContext = workspace["context"]
    empty_workspace = "11111111-1111-1111-1111-111111111111"

    async def seed(uow: UnitOfWork) -> None:
        async with uow.transaction() as tx:
            await tx.execute(text(
                "INSERT INTO workspaces (id, name) VALUES (:id, 'empty')"),
                {"id": empty_workspace},
            )

    db_run(seed)
    summary = db_run(lambda uow: analytics_summary(
        uow, context=AccessContext(
            user_id=context.user_id, workspace_id=empty_workspace, role="owner",
        )
    ))
    assert summary == {
        "exchanges": 0,
        "answered": 0,
        "insufficient_evidence": 0,
        "unanswered_conversations": 0,
        "helpful": 0,
        "unhelpful": 0,
    }
