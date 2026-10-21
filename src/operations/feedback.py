"""Answer feedback and privacy-safe analytics (Task 25).

``record_feedback`` upserts a helpful flag on a stored answer after an
ownership check against the asking user — unknown and foreign request ids
are indistinguishable. Duplicates are last-write-wins, so a double click or
a redelivered POST cannot corrupt the count.

``analytics_summary`` returns counts only (spec §10: diagnostics redact
source content). Unanswered asks classify as insufficient-evidence
exchanges plus messageless conversations — the trace a failed or cancelled
first ask leaves behind completion-gated persistence.
"""

from __future__ import annotations

from sqlalchemy import text

from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext

__all__ = [
    "FeedbackNotFound",
    "analytics_summary",
    "record_feedback",
]


class FeedbackNotFound(Exception):
    """No answer with this request id belongs to this user."""


async def record_feedback(
    uow: UnitOfWork, *, context: AccessContext, request_id: str, helpful: bool
) -> None:
    async with uow.transaction() as transaction:
        answer = (
            await transaction.execute(
                text("SELECT m.conversation_id FROM conversation_messages m "
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
        ).first()
        if answer is None:
            raise FeedbackNotFound("no such answer")
        await transaction.execute(
            text("INSERT INTO answer_feedback "
                 "(request_id, conversation_id, user_id, workspace_id, helpful) "
                 "VALUES (:request_id, :conversation_id, CAST(:user_id AS uuid), "
                 "CAST(:workspace_id AS uuid), :helpful) "
                 "ON CONFLICT (request_id) DO UPDATE SET helpful = EXCLUDED.helpful"),
            {
                "request_id": request_id,
                "conversation_id": str(answer.conversation_id),
                "user_id": context.user_id,
                "workspace_id": context.workspace_id,
                "helpful": helpful,
            },
        )


async def analytics_summary(uow: UnitOfWork, *, context: AccessContext) -> dict[str, int]:
    """Counts only — question and answer text never enter this DTO."""
    async with uow.transaction() as transaction:
        exchanges = (
            await transaction.execute(
                text("SELECT count(*) FILTER (WHERE m.insufficient), "
                     "count(*) FILTER (WHERE NOT m.insufficient) "
                     "FROM conversation_messages m "
                     "JOIN conversations k ON k.id = m.conversation_id "
                     "WHERE m.role = 'assistant' "
                     "AND k.workspace_id = CAST(:workspace_id AS uuid)"),
                {"workspace_id": context.workspace_id},
            )
        ).one()
        conversations = (
            await transaction.execute(
                text("SELECT count(*) FROM conversations k "
                     "WHERE k.workspace_id = CAST(:workspace_id AS uuid) "
                     "AND NOT EXISTS (SELECT 1 FROM conversation_messages m "
                     "WHERE m.conversation_id = k.id)"),
                {"workspace_id": context.workspace_id},
            )
        ).scalar_one()
        feedback = (
            await transaction.execute(
                text("SELECT count(*) FILTER (WHERE f.helpful), "
                     "count(*) FILTER (WHERE NOT f.helpful) "
                     "FROM answer_feedback f "
                     "WHERE f.workspace_id = CAST(:workspace_id AS uuid)"),
                {"workspace_id": context.workspace_id},
            )
        ).one()
    return {
        "exchanges": int(exchanges[0] + exchanges[1]),
        "answered": int(exchanges[1]),
        "insufficient_evidence": int(exchanges[0]),
        "unanswered_conversations": int(conversations),
        "helpful": int(feedback[0]),
        "unhelpful": int(feedback[1]),
    }
