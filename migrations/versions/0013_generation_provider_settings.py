"""Generation provider settings and versioned consent (Task 23).

One row per workspace. The API key is never stored — it arrives through the
``SMART_RAG_GLM_API_KEY`` environment variable at call time — so the row
holds only the endpoint/model choices and the consent the owner recorded.
Reverting to Ollama deletes the row: the absent row *is* the default state.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def postgresql_uuid() -> sa.types.TypeEngine:
    from sqlalchemy.dialects.postgresql import UUID

    return UUID(as_uuid=False)


def upgrade() -> None:
    op.create_table(
        "generation_provider_settings",
        sa.Column("id", postgresql_uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql_uuid(),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column(
            "rewrite_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("consent_version", sa.Text()),
        sa.Column("consented_by", postgresql_uuid(), sa.ForeignKey("users.id")),
        sa.Column("consented_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("generation_provider_settings")
