"""Setup and session API routes (Task 6c).

Session tokens travel in an HttpOnly, SameSite=Strict cookie; mutations
refuse cross-origin requests by comparing hostnames. Failures are generic
(no user enumeration) and dependency failures carry actionable remediation.
``readiness`` and ``uow`` are module-level seams for tests.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.auth import (
    SESSION_TTL_HOURS,
    AuthenticatedUser,
    AuthenticationFailed,
    SessionInvalid,
    authenticate,
    require_session,
    revoke_session,
)
from identity_access.invitations import (
    InvitationInvalid,
    accept_invitation,
    change_password,
)
from identity_access.setup import (
    SetupReadiness,
    WorkspaceAlreadyInitialized,
    check_setup_dependencies,
    initialize_workspace,
)

router = APIRouter(prefix="/api")

SESSION_COOKIE = "smart_rag_session"

_engine: AsyncEngine | None = None


def uow() -> UnitOfWork:
    # NullPool: each request runs on its own event loop (TestClient, workers);
    # pooled connections would stay bound to the loop that created them.
    global _engine
    if _engine is None:
        _engine = create_async_engine(Settings.load().database_url, poolclass=NullPool)
    return UnitOfWork(_engine)


async def readiness() -> SetupReadiness:
    return await check_setup_dependencies(Settings.load())


class OwnerRequest(BaseModel):
    email: str
    password: str
    workspace_name: str = "Workspace"


class SignInRequest(BaseModel):
    email: str
    password: str


def _check_origin(request: Request) -> None:
    """Refuse cross-hostname mutations; port differences stay allowed."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    origin_host = urlparse(origin).hostname
    request_host = urlparse(str(request.url)).hostname
    if origin_host and origin_host != request_host:
        raise HTTPException(status_code=403, detail="cross-origin request refused")


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_HOURS * 3600,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )


async def current_user(request: Request) -> AuthenticatedUser:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        raise HTTPException(status_code=401, detail="not signed in")
    try:
        return await require_session(uow(), token)
    except SessionInvalid:
        raise HTTPException(status_code=401, detail="session expired or revoked") from None


@router.get("/setup/status")
async def setup_status() -> dict:
    ready = await readiness()
    async with uow().transaction() as transaction:
        row = (await transaction.execute(text("SELECT 1 FROM users LIMIT 1"))).first()
    return {
        "initialized": row is not None,
        "dependencies": [asdict(check) for check in ready.checks],
    }


@router.post("/setup/owner", status_code=201)
async def setup_owner(request: Request, response: Response, body: OwnerRequest) -> dict:
    _check_origin(request)
    ready = await readiness()
    if not ready.ok:
        failed = ", ".join(check.name for check in ready.failures())
        raise HTTPException(
            status_code=503,
            detail={
                "message": f"setup dependencies unavailable ({failed}); fix and retry",
                "checks": [asdict(check) for check in ready.checks],
            },
        )
    try:
        created = await initialize_workspace(
            uow(),
            EventWriter(),
            Settings.load(),
            body.email,
            body.password,
            workspace_name=body.workspace_name,
            readiness=ready,
        )
    except WorkspaceAlreadyInitialized:
        raise HTTPException(
            status_code=409,
            detail="workspace already initialized; sign in or run make reset-owner-password",
        ) from None
    session = await authenticate(uow(), EventWriter(), body.email, body.password)
    _set_session_cookie(request, response, session.token)
    return {"workspace_id": created.workspace_id, "user_id": created.user_id}


@router.post("/session")
async def sign_in(request: Request, response: Response, body: SignInRequest) -> dict:
    _check_origin(request)
    try:
        session = await authenticate(uow(), EventWriter(), body.email, body.password)
    except AuthenticationFailed:
        raise HTTPException(
            status_code=401, detail="invalid email or password (or account temporarily locked)"
        ) from None
    _set_session_cookie(request, response, session.token)
    return {"user_id": session.user_id, "expires_at": session.expires_at.isoformat()}


@router.get("/session")
async def session_me(user: Annotated[AuthenticatedUser, Depends(current_user)]) -> dict:
    return {
        "user_id": user.user_id,
        "workspace_id": user.workspace_id,
        "email": user.email,
        "role": user.role,
        "must_change_password": user.must_change_password,
    }


@router.delete("/session", status_code=204)
async def sign_out(
    request: Request,
    response: Response,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
) -> None:
    await revoke_session(uow(), request.cookies[SESSION_COOKIE])
    response.delete_cookie(SESSION_COOKIE, path="/")


class AcceptInvitationRequest(BaseModel):
    token: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/invitations/accept", status_code=201)
async def accept_invitation_route(
    request: Request, response: Response, body: AcceptInvitationRequest
) -> dict:
    _check_origin(request)
    try:
        accepted = await accept_invitation(
            uow(), EventWriter(), token=body.token, password=body.password
        )
    except InvitationInvalid:
        raise HTTPException(
            status_code=400,
            detail="invitation is invalid, expired, or already used",
        ) from None
    session = await authenticate(uow(), EventWriter(), accepted.email, body.password)
    _set_session_cookie(request, response, session.token)
    return {"user_id": session.user_id, "workspace_id": accepted.workspace_id}


@router.post("/session/password", status_code=204)
async def change_password_route(
    request: Request,
    response: Response,
    body: ChangePasswordRequest,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
) -> None:
    _check_origin(request)
    changed = await change_password(
        uow(),
        EventWriter(),
        user_id=user.user_id,
        current_password=body.current_password,
        new_password=body.new_password,
    )
    if not changed:
        raise HTTPException(status_code=403, detail="current password is incorrect")
    # Password rotation revokes all sessions; drop this one from the browser.
    response.delete_cookie(SESSION_COOKIE, path="/")
