"""Source lifecycle: single-active version invariant (Task 16).

Version states become a lifecycle: uploaded -> extracted -> indexed (ready,
awaiting cutover) -> active (retrievable) -> superseded. Existing indexed
versions were the live ones, so the backfill promotes them to active; the
partial unique index then guarantees a source never has two live versions.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE source_versions SET state = 'active' WHERE state = 'indexed'")
    op.create_index(
        "uq_source_versions_active_per_source",
        "source_versions",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_source_versions_active_per_source", "source_versions")
    op.execute("UPDATE source_versions SET state = 'indexed' WHERE state = 'active'")
