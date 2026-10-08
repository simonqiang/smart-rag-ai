"""Conversation history (Task 14a).

Conversations belong to one user in one workspace; every lookup filters on
both, so unknown and foreign IDs are indistinguishable
(``ConversationNotFound``). ``ConversationContext`` carries at most the four
most recent messages — the same bound the spec allows to leave the PC when a
hosted provider is enabled (spec §15).

``record_exchange`` persists the user message, assistant message, recency
touch, and the ``question.asked`` audit event in one transaction. Audit
metadata carries counts and identifiers, never question or answer text.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from foundation.events import AuditEvent, EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext

__all__ = [
    "HISTORY_MESSAGE_LIMIT",
    "ConversationContext",
    "ConversationMessage",
    "ConversationNotFound",
    "create_conversation",
    "delete_conversation",
    "delete_expired_conversations",
    "find_completed_answer",
    "get_conversation",
    "list_conversations",
    "load_context",
    "record_exchange",
    "rename_conversation",
]

HISTORY_MESSAGE_LIMIT = 4


class ConversationNotFound(Exception):
    """The conversation does not exist for this user and workspace."""


@dataclass(frozen=True)
class ConversationMessage:
    role: str  # "user" | "assistant"
    content: str


@dataclass(frozen=True)
class ConversationContext:
    """Bounded window of visible conversation history."""

    messages: tuple[ConversationMessage, ...] = ()

    def __post_init__(self) -> None:
        if len(self.messages) > HISTORY_MESSAGE_LIMIT:
            object.__setattr__(self, "messages", self.messages[-HISTORY_MESSAGE_LIMIT:])

    def appended(self, role: str, content: str) -> ConversationContext:
        return ConversationContext((*self.messages, ConversationMessage(role, content)))

    def as_prompt_lines(self) -> list[str]:
        label = {"user": "User", "assistant": "Assistant"}
        return [f"{label.get(message.role, message.role)}: {message.content}"
                for message in self.messages]


def _own_filter() -> str:
    return ("FROM conversations k "
            "WHERE k.id = CAST(:conversation_id AS uuid) "
            "AND k.workspace_id = CAST(:workspace_id AS uuid) "
            "AND k.user_id = CAST(:user_id AS uuid) ")


def _params(context: AccessContext, conversation_id: str) -> dict:
    return {
        "conversation_id": conversation_id,
        "workspace_id": context.workspace_id,
        "user_id": context.user_id,
    }


async def create_conversation(
    uow: UnitOfWork, context: AccessContext, *, title: str
) -> str:
    conversation_id = str(uuid.uuid4())
    async with uow.transaction() as transaction:
        await transaction.execute(
            text("INSERT INTO conversations (id, workspace_id, user_id, title) "
                 "VALUES (:id, CAST(:workspace_id AS uuid), CAST(:user_id AS uuid), :title)"),
            {
                "id": conversation_id,
                "workspace_id": context.workspace_id,
                "user_id": context.user_id,
                "title": title,
            },
        )
    return conversation_id


async def record_exchange(
    uow: UnitOfWork,
    events: EventWriter,
    *,
    context: AccessContext,
    conversation_id: str,
    question: str,
    answer_text: str,
    citations: list[dict],
    language: str,
    request_id: str,
    insufficient_evidence: bool,
) -> None:
    """Persist both messages, recency, and the audit event atomically."""
    async with uow.transaction() as transaction:
        await transaction.execute(
            text("INSERT INTO conversation_messages "
                 "(id, conversation_id, role, content) "
                 "VALUES (:id, CAST(:conversation_id AS uuid), 'user', :content)"),
            {"id": str(uuid.uuid4()),
             "conversation_id": conversation_id, "content": question},
        )
        await transaction.execute(
            text("INSERT INTO conversation_messages "
                 "(id, conversation_id, role, content, citations, language, "
                 "request_id, insufficient) "
                 "VALUES (:id, CAST(:conversation_id AS uuid), 'assistant', :content, "
                 "CAST(:citations AS jsonb), :language, :request_id, :insufficient)"),
            {
                "id": str(uuid.uuid4()),
                "conversation_id": conversation_id,
                "content": answer_text,
                "citations": _json(citations),
                "language": language,
                "request_id": request_id,
                "insufficient": insufficient_evidence,
            },
        )
        await transaction.execute(
            text("UPDATE conversations SET updated_at = now() "
                 "WHERE id = CAST(:conversation_id AS uuid)"),
            {"conversation_id": conversation_id},
        )
        await events.record(
            AuditEvent(
                type="question.asked",
                actor=context.user_id,
                subject=conversation_id,
                metadata={
                    "request_id": request_id,
                    "language": language,
                    "citations": len(citations),
                    "insufficient_evidence": insufficient_evidence,
                },
            ),
            transaction,
        )


async def _own_conversation(uow: UnitOfWork, context: AccessContext,
                            conversation_id: str) -> dict:
    async with uow.transaction() as transaction:
        row = (
            await transaction.execute(
                text(f"SELECT id, title, created_at, updated_at {_own_filter()}"),
                _params(context, conversation_id),
            )
        ).mappings().first()
    if row is None:
        raise ConversationNotFound("no such conversation")
    return dict(row)


async def load_context(
    uow: UnitOfWork, *, context: AccessContext, conversation_id: str
) -> ConversationContext:
    await _own_conversation(uow, context, conversation_id)
    async with uow.transaction() as transaction:
        rows = (
            await transaction.execute(
                text("SELECT role, content FROM conversation_messages "
                     "WHERE conversation_id = CAST(:conversation_id AS uuid) "
                     "ORDER BY number DESC LIMIT :limit"),
                {"conversation_id": conversation_id, "limit": HISTORY_MESSAGE_LIMIT},
            )
        ).all()
    # Newest-first from SQL, oldest-first for the prompt window.
    return ConversationContext(messages=tuple(
        ConversationMessage(role=role, content=content) for role, content in reversed(rows)
    ))


async def get_conversation(
    uow: UnitOfWork, *, context: AccessContext, conversation_id: str
) -> dict:
    conversation = await _own_conversation(uow, context, conversation_id)
    async with uow.transaction() as transaction:
        rows = (
            await transaction.execute(
                text("SELECT role, content, citations, language, request_id, insufficient, "
                     "created_at FROM conversation_messages "
                     "WHERE conversation_id = CAST(:conversation_id AS uuid) "
                     "ORDER BY number"),
                {"conversation_id": conversation_id},
            )
        ).mappings().all()
    conversation["messages"] = [
        {
            "role": row["role"],
            "content": row["content"],
            "citations": row["citations"],
            "language": row["language"],
            "request_id": row["request_id"],
            "insufficient": bool(row["insufficient"]),
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]
    conversation["id"] = str(conversation["id"])
    return conversation


async def list_conversations(
    uow: UnitOfWork, *, context: AccessContext
) -> list[dict]:
    async with uow.transaction() as transaction:
        rows = (
            await transaction.execute(
                text("SELECT id, title, created_at, updated_at FROM conversations "
                     "WHERE workspace_id = CAST(:workspace_id AS uuid) "
                     "AND user_id = CAST(:user_id AS uuid) "
                     "ORDER BY updated_at DESC"),
                {"workspace_id": context.workspace_id, "user_id": context.user_id},
            )
        ).mappings().all()
    return [
        {
            "id": str(row["id"]),
            "title": row["title"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }
        for row in rows
    ]


async def rename_conversation(
    uow: UnitOfWork, *, context: AccessContext, conversation_id: str, title: str
) -> None:
    await _own_conversation(uow, context, conversation_id)
    async with uow.transaction() as transaction:
        await transaction.execute(
            text("UPDATE conversations SET title = :title "
                 "WHERE id = CAST(:conversation_id AS uuid)"),
            {"conversation_id": conversation_id, "title": title},
        )


async def delete_conversation(
    uow: UnitOfWork, *, context: AccessContext, conversation_id: str
) -> None:
    await _own_conversation(uow, context, conversation_id)
    async with uow.transaction() as transaction:
        await transaction.execute(
            text("DELETE FROM conversations WHERE id = CAST(:conversation_id AS uuid)"),
            {"conversation_id": conversation_id},
        )


async def delete_expired_conversations(uow: UnitOfWork, *, cutoff: datetime) -> int:
    """Retention hook for Task 17A: removes conversations idle past ``cutoff``."""
    async with uow.transaction() as transaction:
        result = await transaction.execute(
            text("DELETE FROM conversations WHERE updated_at < :cutoff"),
            {"cutoff": cutoff},
        )
    return result.rowcount


async def find_completed_answer(
    uow: UnitOfWork, *, context: AccessContext, request_id: str
) -> dict | None:
    """Resume source: the assistant message recorded for this request, if any."""
    async with uow.transaction() as transaction:
        row = (
            await transaction.execute(
                text("SELECT m.conversation_id, m.content, m.citations, m.language, "
                     "m.insufficient FROM conversation_messages m "
                     "JOIN conversations k ON k.id = m.conversation_id "
                     "WHERE m.request_id = :request_id AND m.role = 'assistant' "
                     "AND k.workspace_id = CAST(:workspace_id AS uuid) "
                     "AND k.user_id = CAST(:user_id AS uuid)"),
                {
                    "request_id": request_id,
                    "workspace_id": context.workspace_id,
                    "user_id": context.user_id,
                },
            )
        ).mappings().first()
    if row is None:
        return None
    return {
        "conversation_id": str(row["conversation_id"]),
        "content": row["content"],
        "citations": row["citations"],
        "language": row["language"],
        "insufficient": bool(row["insufficient"]),
    }


def _json(value: list[dict]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
