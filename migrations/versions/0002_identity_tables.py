"""Identity tables: workspaces, users, sessions, invitations (Task 6a).

Revision ID: 0002_identity
Revises: 0001_foundation
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql_uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "users",
        sa.Column("id", postgresql_uuid(), primary_key=True),
        sa.Column("workspace_id", postgresql_uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="member"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_users_workspace_email", "users", ["workspace_id", "email"], unique=True)
    op.create_table(
        "sessions",
        sa.Column("id", postgresql_uuid(), primary_key=True),
        sa.Column("user_id", postgresql_uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_sessions_token_hash", "sessions", ["token_hash"], unique=True)
    op.create_table(
        "invitations",
        sa.Column("id", postgresql_uuid(), primary_key=True),
        sa.Column("workspace_id", postgresql_uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="member"),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("collections", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("invited_by", postgresql_uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_invitations_token_hash", "invitations", ["token_hash"], unique=True)


def postgresql_uuid() -> sa.types.TypeEngine:
    from sqlalchemy.dialects.postgresql import UUID

    return UUID(as_uuid=False)


def downgrade() -> None:
    op.drop_table("invitations")
    op.drop_table("sessions")
    op.drop_table("users")
    op.drop_table("workspaces")
