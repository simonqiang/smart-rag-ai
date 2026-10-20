"""Persist the last typed job failure reason for operations views (Task 22).

``jobs.last_error`` records the sanitized reason workers pass to
``JobClaims.fail``/``retry`` — never raw exception text — so operators can
see why a job failed without a second audit-writing path.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("last_error", sa.Text()))


def downgrade() -> None:
    op.drop_column("jobs", "last_error")
