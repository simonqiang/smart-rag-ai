"""Conversations and messages for grounded asking (Task 14a).

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def postgresql_uuid() -> sa.types.TypeEngine:
    from sqlalchemy.dialects.postgresql import UUID

    return UUID(as_uuid=False)


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql_uuid(), primary_key=True),
        sa.Column("workspace_id", postgresql_uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", postgresql_uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_conversations_user", "conversations", ["workspace_id", "user_id", "updated_at"]
    )
    op.create_table(
        "conversation_messages",
        sa.Column("id", postgresql_uuid(), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql_uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Monotonic insert order; now() is transaction-scoped so timestamps tie.
        sa.Column("number", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("insufficient", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_conversation_messages_conversation",
        "conversation_messages",
        ["conversation_id", "number"],
    )
    op.create_index("ix_conversation_messages_request", "conversation_messages", ["request_id"])


def downgrade() -> None:
    op.drop_table("conversation_messages")
    op.drop_table("conversations")
