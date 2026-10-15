"""Retention policies: per-workspace purge windows (Task 17A).

Defaults follow spec §15 (owner confirmed 2026-10-05): superseded versions
and retired pages 30 days, conversations 90 days. Workspaces without a row
use the defaults, so the scheduled purge works before the owner ever opens
the retention screen.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def postgresql_uuid() -> sa.types.TypeEngine:
    from sqlalchemy.dialects.postgresql import UUID

    return UUID(as_uuid=False)


def upgrade() -> None:
    op.create_table(
        "retention_policies",
        sa.Column("workspace_id", postgresql_uuid(), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("superseded_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("conversation_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("retention_policies")
