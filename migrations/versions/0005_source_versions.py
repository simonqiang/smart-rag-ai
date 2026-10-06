"""Source versions: immutable uploaded originals (Task 9a).

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def postgresql_uuid() -> sa.types.TypeEngine:
    from sqlalchemy.dialects.postgresql import UUID

    return UUID(as_uuid=False)


def upgrade() -> None:
    op.create_table(
        "source_versions",
        sa.Column("id", postgresql_uuid(), primary_key=True),
        sa.Column("workspace_id", postgresql_uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("source_id", postgresql_uuid(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("object_sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="uploaded"),
        sa.Column("created_by", postgresql_uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_source_versions_source", "source_versions", ["source_id"])
    op.create_index(
        "ix_source_versions_workspace_sha", "source_versions", ["workspace_id", "object_sha256"]
    )


def downgrade() -> None:
    op.drop_table("source_versions")
