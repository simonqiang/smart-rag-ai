"""Task 7b: role hierarchy and collection access rules."""

import pytest

from identity_access.authorization import (
    AccessContext,
    AccessDenied,
    ProtectedResource,
    authorize,
    can_access_collection,
)


def _context(role: str, workspace_id: str = "ws-1", user_id: str = "u-1") -> AccessContext:
    return AccessContext(user_id=user_id, workspace_id=workspace_id, role=role)


def test_member_cannot_list_users_admin_and_owner_can() -> None:
    with pytest.raises(AccessDenied):
        authorize("user.list", ProtectedResource("ws-1"), _context("member"))
    authorize("user.list", ProtectedResource("ws-1"), _context("admin"))
    authorize("user.list", ProtectedResource("ws-1"), _context("owner"))


def test_admin_manages_members_but_not_admins_or_owners() -> None:
    admin = _context("admin")
    authorize("user.reset_password", ProtectedResource("ws-1", "member"), admin)
    with pytest.raises(AccessDenied):
        authorize("user.reset_password", ProtectedResource("ws-1", "admin"), admin)
    with pytest.raises(AccessDenied):
        authorize("user.reset_password", ProtectedResource("ws-1", "owner"), admin)


def test_owner_manages_admins_and_members_but_not_owners() -> None:
    owner = _context("owner")
    authorize("user.disable", ProtectedResource("ws-1", "admin"), owner)
    authorize("user.disable", ProtectedResource("ws-1", "member"), owner)
    with pytest.raises(AccessDenied):
        authorize("user.disable", ProtectedResource("ws-1", "owner"), owner)


def test_admin_cannot_invite_admin() -> None:
    admin = _context("admin")
    authorize("user.invite", ProtectedResource("ws-1", "member"), admin)
    with pytest.raises(AccessDenied):
        authorize("user.invite", ProtectedResource("ws-1", "admin"), admin)


def test_cross_workspace_resource_is_denied_even_for_owner() -> None:
    with pytest.raises(AccessDenied):
        authorize(
            "user.reset_password",
            ProtectedResource("ws-other", "member"),
            _context("owner"),
        )


def test_disable_cannot_target_self() -> None:
    with pytest.raises(AccessDenied):
        authorize(
            "user.disable",
            ProtectedResource("ws-1", "member", resource_id="u-1"),
            _context("admin", user_id="u-1"),
        )


def test_unknown_action_is_rejected() -> None:
    with pytest.raises(ValueError):
        authorize("user.nuke", ProtectedResource("ws-1"), _context("owner"))


def test_collection_access_is_grant_list_membership() -> None:
    # Owner/admin contexts carry their enumerated workspace collections;
    # members carry grants. Either way the check is membership.
    scoped = AccessContext(
        user_id="u-1", workspace_id="ws-1", role="member", collection_ids=["col-a"]
    )
    assert can_access_collection(scoped, "col-a") is True
    assert can_access_collection(scoped, "col-b") is False
    empty = AccessContext(user_id="u-1", workspace_id="ws-1", role="owner")
    assert can_access_collection(empty, "col-a") is False
