"""Source catalog tables: collections and sources (Task 8a).

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def postgresql_uuid() -> sa.types.TypeEngine:
    from sqlalchemy.dialects.postgresql import UUID

    return UUID(as_uuid=False)


def upgrade() -> None:
    op.create_table(
        "collections",
        sa.Column("id", postgresql_uuid(), primary_key=True),
        sa.Column("workspace_id", postgresql_uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "name", name="uq_collections_workspace_name"),
    )
    op.create_table(
        "sources",
        sa.Column("id", postgresql_uuid(), primary_key=True),
        sa.Column("workspace_id", postgresql_uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("collection_id", postgresql_uuid(), sa.ForeignKey("collections.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_by", postgresql_uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sources_collection", "sources", ["collection_id"])
    op.create_index("ix_collections_workspace", "collections", ["workspace_id"])


def downgrade() -> None:
    op.drop_table("sources")
    op.drop_table("collections")
