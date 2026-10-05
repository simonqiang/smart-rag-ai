"""Collection grants: per-user collection access (Task 7a).

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def postgresql_uuid() -> sa.types.TypeEngine:
    from sqlalchemy.dialects.postgresql import UUID

    return UUID(as_uuid=False)


def upgrade() -> None:
    op.create_table(
        "collection_grants",
        sa.Column("id", postgresql_uuid(), primary_key=True),
        sa.Column("workspace_id", postgresql_uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", postgresql_uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("collection_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "collection_id", name="uq_grants_user_collection"),
    )
    op.create_index("ix_grants_collection", "collection_grants", ["collection_id"])


def downgrade() -> None:
    op.drop_table("collection_grants")
