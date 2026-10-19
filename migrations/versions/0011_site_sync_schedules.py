"""Site sync schedules: crawl definitions and freshness state (Task 21).

One row per synchronized website. ``source_id`` fills in at the first
successful stage and cascades away when Task 17 purges the source row, so a
deleted site never re-stages itself; the claim predicate additionally
requires the linked source to be ``active`` while it still exists.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def postgresql_uuid() -> sa.types.TypeEngine:
    from sqlalchemy.dialects.postgresql import UUID

    return UUID(as_uuid=False)


def upgrade() -> None:
    op.create_table(
        "site_sync_schedules",
        sa.Column("id", postgresql_uuid(), primary_key=True),
        sa.Column("workspace_id", postgresql_uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("collection_id", postgresql_uuid(), sa.ForeignKey("collections.id"), nullable=False),
        sa.Column("source_id", postgresql_uuid(),
                  sa.ForeignKey("sources.id", ondelete="CASCADE")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("start_url", sa.Text(), nullable=False),
        sa.Column("sitemap_url", sa.Text()),
        sa.Column("allowed_prefixes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("last_state", sa.Text()),
        sa.Column("last_detail", sa.Text()),
        sa.Column("created_by", postgresql_uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("site_sync_schedules")
