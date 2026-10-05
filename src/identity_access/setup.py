"""First-run workspace initialization (Task 6a).

``create_first_owner`` creates the workspace and its first owner atomically
and exactly once: the first-owner exclusivity check, both inserts, and the
``workspace.initialized`` audit event share one transaction, so a failure
anywhere leaves the database untouched.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text

from foundation.events import AuditEvent, EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.passwords import hash_password


class WorkspaceAlreadyInitialized(Exception):
    """A first owner already exists; only host-local recovery may proceed."""


@dataclass(frozen=True)
class CreatedOwner:
    workspace_id: str
    user_id: str


async def create_first_owner(
    uow: UnitOfWork,
    writer: EventWriter,
    email: str,
    password: str,
    workspace_name: str = "Workspace",
) -> CreatedOwner:
    async with uow.transaction() as transaction:
        existing = await transaction.execute(text("SELECT 1 FROM users LIMIT 1"))
        if existing.first() is not None:
            raise WorkspaceAlreadyInitialized(
                "workspace already has a user; use 'make reset-owner-password' to recover"
            )
        workspace_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        await transaction.execute(
            text("INSERT INTO workspaces (id, name) VALUES (:id, :name)"),
            {"id": workspace_id, "name": workspace_name},
        )
        await transaction.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, role, status) "
                "VALUES (:id, :workspace_id, :email, :password_hash, 'owner', 'active')"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": email,
                "password_hash": hash_password(password),
            },
        )
        await writer.record(
            AuditEvent(type="workspace.initialized", actor=email, subject=workspace_id),
            transaction,
        )
    return CreatedOwner(workspace_id=workspace_id, user_id=user_id)
