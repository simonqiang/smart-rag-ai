"""Role-based authorization and collection access (Task 7b).

Roles are a strict ladder: owner > admin > member. An actor may manage a
target only when the target ranks strictly below them, so admins manage
members, owners manage admins and members, and nobody manages an owner
(host recovery in Task 6e is the only owner reset path). ``authorize``
raises the same ``AccessDenied`` for every denial reason — callers cannot
distinguish rule from workspace mismatches, and routes return an identical
generic 404 for unknown and cross-workspace targets so object IDs cannot
be probed.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from foundation.unit_of_work import UnitOfWork

ROLES = {"owner": 0, "admin": 1, "member": 2}

# Minimum role per administrative action.
MINIMUM_ROLE = {
    "user.list": "admin",
    "user.invite": "admin",
    "user.set_grants": "admin",
    "user.reset_password": "admin",
    "user.disable": "admin",
    "invitation.revoke": "admin",
}

# Actions that may never target the actor themselves.
SELF_FORBIDDEN = {"user.disable"}


class AccessDenied(Exception):
    """The context may not perform this action on this resource."""


@dataclass(frozen=True)
class AccessContext:
    user_id: str
    workspace_id: str
    role: str
    collection_ids: list[str] | None = None  # None = unrestricted (owner/admin)


@dataclass(frozen=True)
class ProtectedResource:
    workspace_id: str
    role: str = "member"
    resource_id: str | None = None


def _rank(role: str) -> int:
    return ROLES.get(role, len(ROLES))


def authorize(action: str, resource: ProtectedResource, context: AccessContext) -> None:
    if action not in MINIMUM_ROLE:
        raise ValueError(f"unknown action: {action}")
    if _rank(context.role) > _rank(MINIMUM_ROLE[action]):
        raise AccessDenied(action)
    if resource.workspace_id != context.workspace_id:
        raise AccessDenied(action)
    # Targets must rank strictly below the actor; list/revoke resources use the
    # "member" default, which every qualifying actor outranks.
    if _rank(resource.role) <= _rank(context.role):
        raise AccessDenied(action)
    if action in SELF_FORBIDDEN and resource.resource_id == context.user_id:
        raise AccessDenied(action)


def can_access_collection(context: AccessContext, collection_id: str) -> bool:
    if context.collection_ids is None:
        return True
    return collection_id in context.collection_ids


async def accessible_collection_ids(
    uow: UnitOfWork, *, user_id: str, workspace_id: str
) -> list[str] | None:
    """Grant-scoped collection IDs, or None when the role is unrestricted.

    Unknown users resolve to no access, never a wildcard.
    """
    async with uow.transaction() as transaction:
        role = (
            await transaction.execute(
                text(
                    "SELECT role FROM users "
                    "WHERE id = :id AND workspace_id = :ws AND status = 'active'"
                ),
                {"id": user_id, "ws": workspace_id},
            )
        ).scalar_one_or_none()
        if role is None or str(role) not in ROLES:
            return []
        if str(role) != "member":
            return None  # ponytail: wildcard until a collections table exists (Task 8) to enumerate
        rows = (
            await transaction.execute(
                text(
                    "SELECT collection_id FROM collection_grants "
                    "WHERE user_id = :id ORDER BY collection_id"
                ),
                {"id": user_id},
            )
        ).scalars().all()
        return list(rows)
