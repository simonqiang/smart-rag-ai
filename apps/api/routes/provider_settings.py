"""Generation provider settings routes (Task 23c).

Provider state is visible to every signed-in user (the Ask page badges it);
changes are owner-only. The API key never travels through this API — the
GLM adapters read ``SMART_RAG_GLM_API_KEY`` from the server environment at
call time. Probe and activate round-trip the candidate endpoint through an
injectable transport (``_transport``), so tests never leave the machine.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ai_providers.contracts import ProviderError
from ai_providers.glm import GLMConfig, _Transport
from ai_providers.provider_settings import (
    CONSENT_VERSION,
    ConsentVersionMismatch,
    GenerationProviderState,
    activate_generation_provider,
    get_generation_provider_state,
    revert_generation_provider,
    test_provider,
)
from apps.api.routes.auth import _check_origin, uow
from apps.api.routes.sources import catalog_context
from apps.api.routes.users import ready_user
from foundation.events import EventWriter
from identity_access.auth import AuthenticatedUser
from identity_access.authorization import AccessDenied, ProtectedResource, authorize

router = APIRouter(prefix="/api/settings/ai-provider")

# Test seam: None = the production urllib transport (Task 23a).
_transport: _Transport | None = None

# Owner-fixable configuration mistakes map to 400; endpoint trouble to 503.
_KEY_REASONS = ("invalid_key", "missing_key", "missing_model")
_REASON_MESSAGES = {
    "invalid_key": "the GLM endpoint rejected the API key; check SMART_RAG_GLM_API_KEY",
    "missing_key": "set SMART_RAG_GLM_API_KEY in the server environment and retry",
    "missing_model": "the GLM endpoint does not know this model; check the model name",
    "unreachable": "the GLM endpoint is unreachable; check the endpoint URL and retry",
    "timeout": "the GLM endpoint did not answer in time; check the endpoint and retry",
}


class CandidateConfig(BaseModel):
    endpoint: str = Field(min_length=1)
    model: str = Field(min_length=1)


class ActivationRequest(CandidateConfig):
    rewrite_enabled: bool = False
    consent_version: str = Field(min_length=1)


def _payload(state: GenerationProviderState) -> dict:
    return {
        "provider": state.provider,
        "endpoint": state.endpoint,
        "model": state.model,
        "rewrite_enabled": state.rewrite_enabled,
        "consent_version": state.consent_version,
        "consented_at": state.consented_at.isoformat() if state.consented_at else None,
        # The version the settings screen's consent checkbox binds to; the
        # stored consent_version above is what was accepted, not what is due.
        "consent_version_current": CONSENT_VERSION,
    }


def _require_owner(context) -> None:
    try:
        authorize("provider.update", ProtectedResource(context.workspace_id), context)
    except AccessDenied:
        raise HTTPException(status_code=403, detail="owner role required") from None


def _provider_failure(exc: ProviderError) -> HTTPException:
    message = _REASON_MESSAGES.get(
        exc.reason, "the GLM endpoint returned an invalid response; check the endpoint URL"
    )
    return HTTPException(
        status_code=400 if exc.reason in _KEY_REASONS else 503,
        detail={"reason": exc.reason, "message": f"GLM probe failed: {message}"},
    )


@router.get("")
async def show(
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    context = await catalog_context(user)
    state = await get_generation_provider_state(uow(), workspace_id=context.workspace_id)
    return _payload(state)


@router.post("/test")
async def test(
    body: CandidateConfig,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    _require_owner(context)
    try:
        await test_provider(
            context, GLMConfig(base_url=body.endpoint, model=body.model),
            transport=_transport,
        )
    except ProviderError as exc:
        raise _provider_failure(exc) from None
    return {"ok": True}


@router.post("/activate")
async def activate(
    body: ActivationRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    _require_owner(context)
    try:
        state = await activate_generation_provider(
            uow(), EventWriter(), context=context,
            config=GLMConfig(base_url=body.endpoint, model=body.model),
            rewrite_enabled=body.rewrite_enabled,
            consent_version=body.consent_version,
            transport=_transport,
        )
    except ConsentVersionMismatch:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "consent_required",
                "message": "review and accept the current consent text "
                           f"(version {CONSENT_VERSION}) to activate GLM",
            },
        ) from None
    except ProviderError as exc:
        raise _provider_failure(exc) from None
    return _payload(state)


@router.post("/revert")
async def revert(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    _require_owner(context)
    state = await revert_generation_provider(uow(), EventWriter(), context=context)
    return _payload(state)
