"""Managed backups and the backup retention window (Task 24).

One row per encrypted backup file in ``backup_dir``; ``source_ids`` drives
the deletion-aware inventory port (Task 5): a permanent source deletion
purges every backup still containing it before deletion is verified.
``retention_policies.backup_days`` extends the Task 17A windows — managed
backups older than the window are purged by the same scheduled pass.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def postgresql_uuid() -> sa.types.TypeEngine:
    from sqlalchemy.dialects.postgresql import UUID

    return UUID(as_uuid=False)


def upgrade() -> None:
    op.create_table(
        "managed_backups",
        sa.Column("id", postgresql_uuid(), primary_key=True),
        sa.Column("filename", sa.Text(), nullable=False, unique=True),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "retention_policies",
        sa.Column("backup_days", sa.Integer(), nullable=False, server_default="30"),
    )


def downgrade() -> None:
    op.drop_column("retention_policies", "backup_days")
    op.drop_table("managed_backups")
