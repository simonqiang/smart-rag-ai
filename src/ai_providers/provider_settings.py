"""Generation provider choice with versioned consent (Task 23b).

The workspace generation provider is Ollama until the owner activates GLM.
Activation is explicit and evidence-backed: the owner must acknowledge the
consent text version they actually saw (``CONSENT_VERSION``), and the
candidate endpoint must pass a live round-trip before anything is persisted
(test-before-activate). The probe sends only a fixed string — never
workspace content. The API key is a secret *reference* read from the
environment at call time; the settings row stores endpoint/model/flags only.

Switching never touches embeddings (bge-m3/Ollama stays the only embedding
path), so no re-index is needed; this module writes no outbox rows. GLM
failure surfaces as the ask route's typed error frame — there is no
fallback path back to Ollama.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_providers.contracts import GenerationRequest, GenerationResult
from ai_providers.glm import GLMConfig, GLMGenerationProvider, _Transport
from foundation.events import AuditEvent, EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext, ProtectedResource, authorize

# Bump when the consent text changes; activation must carry exactly this
# version, so a stored consent can never silently cover new terms.
CONSENT_VERSION = "2026-10-20"

# The probe round-trip prompt; deliberately content-free.
PROBE_PROMPT = "Reply with the single word: ready"


class ConsentVersionMismatch(Exception):
    """Activation requires acknowledgement of the current consent version."""


@dataclass(frozen=True)
class GenerationProviderState:
    """What signed-in users may see; ``None`` fields mean "not activated"."""

    provider: str = "ollama"
    endpoint: str | None = None
    model: str | None = None
    rewrite_enabled: bool = False
    consent_version: str | None = None
    consented_at: datetime | None = None


_STATE_QUERY = text(
    "SELECT provider, endpoint, model, rewrite_enabled, consent_version, consented_at "
    "FROM generation_provider_settings WHERE workspace_id = :workspace_id"
)


async def get_generation_provider_state(
    uow: UnitOfWork, *, workspace_id: str
) -> GenerationProviderState:
    async with uow.transaction() as transaction:
        row = (
            (await transaction.execute(_STATE_QUERY, {"workspace_id": workspace_id}))
            .mappings()
            .first()
        )
    if row is None:
        return GenerationProviderState()
    return GenerationProviderState(
        provider=str(row["provider"]),
        endpoint=row["endpoint"] and str(row["endpoint"]),
        model=row["model"] and str(row["model"]),
        rewrite_enabled=bool(row["rewrite_enabled"]),
        consent_version=row["consent_version"] and str(row["consent_version"]),
        consented_at=row["consented_at"],
    )


async def test_provider(
    context: AccessContext,
    config: GLMConfig,
    *,
    transport: _Transport | None = None,
    api_key: str | None = None,
) -> GenerationResult:
    """Owner-only live round-trip of a candidate config; persists nothing.

    The probe prompt is a fixed string, so a failed or malicious endpoint
    never receives workspace content. Typed provider errors propagate.
    """
    authorize("provider.update", ProtectedResource(context.workspace_id), context)
    provider = GLMGenerationProvider(config, transport=transport, api_key=api_key)
    return provider.generate(GenerationRequest(prompt=PROBE_PROMPT))


async def activate_generation_provider(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    context: AccessContext,
    config: GLMConfig,
    rewrite_enabled: bool,
    consent_version: str,
    transport: _Transport | None = None,
    api_key: str | None = None,
) -> GenerationProviderState:
    """Owner-only GLM activation: consent match, test, then one transaction.

    The HTTP probe runs before the transaction opens, so a slow endpoint
    never holds a database transaction; a failed probe raises before any
    row or audit exists.
    """
    authorize("provider.update", ProtectedResource(context.workspace_id), context)
    if consent_version != CONSENT_VERSION:
        raise ConsentVersionMismatch(
            f"consent required for version {CONSENT_VERSION}, got {consent_version!r}"
        )
    await test_provider(context, config, transport=transport, api_key=api_key)
    async with uow.transaction() as transaction:
        await _upsert(
            transaction,
            context=context,
            config=config,
            rewrite_enabled=rewrite_enabled,
            consent_version=consent_version,
        )
        await writer.record(
            AuditEvent(
                type="provider.activated",
                actor=context.user_id,
                subject=context.workspace_id,
                metadata={
                    "provider": "glm",
                    "endpoint": config.base_url,
                    "model": config.model,
                    "rewrite_enabled": rewrite_enabled,
                    "consent_version": consent_version,
                },
            ),
            transaction,
        )
    return await get_generation_provider_state(uow, workspace_id=context.workspace_id)


async def revert_generation_provider(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    context: AccessContext,
) -> GenerationProviderState:
    """Owner-only revert to the Ollama default; the row is the GLM state."""
    authorize("provider.update", ProtectedResource(context.workspace_id), context)
    async with uow.transaction() as transaction:
        await transaction.execute(
            text("DELETE FROM generation_provider_settings WHERE workspace_id = :ws"),
            {"ws": context.workspace_id},
        )
        await writer.record(
            AuditEvent(
                type="provider.reverted",
                actor=context.user_id,
                subject=context.workspace_id,
                metadata={"provider": "ollama"},
            ),
            transaction,
        )
    return GenerationProviderState()


async def _upsert(
    transaction: AsyncConnection,
    *,
    context: AccessContext,
    config: GLMConfig,
    rewrite_enabled: bool,
    consent_version: str,
) -> None:
    await transaction.execute(
        text(
            "INSERT INTO generation_provider_settings "
            "(id, workspace_id, provider, endpoint, model, rewrite_enabled, "
            " consent_version, consented_by, consented_at, updated_at) "
            "VALUES (:id, :ws, 'glm', :endpoint, :model, :rewrite, :consent, "
            ":by, now(), now()) "
            "ON CONFLICT (workspace_id) DO UPDATE SET "
            "provider = 'glm', endpoint = :endpoint, model = :model, "
            "rewrite_enabled = :rewrite, consent_version = :consent, "
            "consented_by = :by, consented_at = now(), updated_at = now()"
        ),
        {
            "id": str(uuid.uuid4()),
            "ws": context.workspace_id,
            "endpoint": config.base_url,
            "model": config.model,
            "rewrite": rewrite_enabled,
            "consent": consent_version,
            "by": context.user_id,
        },
    )
