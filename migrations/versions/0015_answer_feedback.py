"""Answer feedback for analytics (Task 25).

One row per feedback click on a stored answer, keyed by the ask request id.
Cascades with the conversation so retention and Task 17 deletion remove the
signal with the exchange — no orphan telemetry.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def postgresql_uuid() -> sa.types.TypeEngine:
    from sqlalchemy.dialects.postgresql import UUID

    return UUID(as_uuid=False)


def upgrade() -> None:
    op.create_table(
        "answer_feedback",
        sa.Column("request_id", sa.Text(), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql_uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", postgresql_uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            postgresql_uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("helpful", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index(
        "ix_answer_feedback_workspace", "answer_feedback", ["workspace_id", "helpful"]
    )


def downgrade() -> None:
    op.drop_table("answer_feedback")
