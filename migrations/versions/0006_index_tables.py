"""Knowledge index: generations, chunk vectors, keyword vectors (Task 12).

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def postgresql_uuid() -> sa.types.TypeEngine:
    from sqlalchemy.dialects.postgresql import UUID

    return UUID(as_uuid=False)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "index_generations",
        sa.Column("id", postgresql_uuid(), primary_key=True),
        sa.Column("workspace_id", postgresql_uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("source_version_id", postgresql_uuid(), sa.ForeignKey("source_versions.id"), nullable=False),
        sa.Column("compatibility_key", sa.Text(), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="staging"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_index_generations_version_state", "index_generations",
        ["source_version_id", "state"],
    )
    op.create_table(
        "index_chunks",
        sa.Column("id", postgresql_uuid(), primary_key=True),
        sa.Column("generation_id", postgresql_uuid(), sa.ForeignKey("index_generations.id"), nullable=False),
        sa.Column("workspace_id", postgresql_uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("source_id", postgresql_uuid(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("source_version_id", postgresql_uuid(), sa.ForeignKey("source_versions.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("block_start", sa.Integer(), nullable=False),
        sa.Column("block_end", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # pgvector and tsvector types are created server-side; Alembic has no
    # portable dialect type for them.
    op.execute("ALTER TABLE index_chunks ADD COLUMN embedding vector NOT NULL")
    op.execute("ALTER TABLE index_chunks ADD COLUMN keywords tsvector NOT NULL")
    op.execute(
        "CREATE INDEX ix_index_chunks_generation ON index_chunks (generation_id, ordinal)"
    )
    op.execute("CREATE INDEX ix_index_chunks_keywords ON index_chunks USING gin (keywords)")


def downgrade() -> None:
    op.drop_table("index_chunks")
    op.drop_table("index_generations")
