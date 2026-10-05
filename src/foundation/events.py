"""Transactional audit and outbox events (Task 5).

``EventWriter.record`` writes an ``AuditEvent`` (operator-visible history) or
an ``OutboxEvent`` (reliable dispatch) inside the caller's open transaction,
so an event can never commit without the state change it describes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True)
class AuditEvent:
    type: str
    actor: str
    subject: str
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OutboxEvent:
    topic: str
    payload: dict


class EventWriter:
    async def record(self, event: AuditEvent | OutboxEvent, transaction: AsyncConnection) -> None:
        if isinstance(event, AuditEvent):
            await transaction.execute(
                text(
                    "INSERT INTO audit_events (id, type, actor, subject, metadata, created_at) "
                    "VALUES (:id, :type, :actor, :subject, CAST(:metadata AS jsonb), now())"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "type": event.type,
                    "actor": event.actor,
                    "subject": event.subject,
                    "metadata": _json(event.metadata),
                },
            )
            return
        await transaction.execute(
            text(
                "INSERT INTO outbox (id, topic, payload, created_at) "
                "VALUES (:id, :topic, CAST(:payload AS jsonb), now())"
            ),
            {
                "id": str(uuid.uuid4()),
                "topic": event.topic,
                "payload": _json(event.payload),
            },
        )


def _json(value: dict) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
