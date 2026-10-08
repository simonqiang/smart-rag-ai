"""Hybrid search route (Task 13).

Retrieval runs under the caller's grant-resolved context; provider failures
map to actionable 503s, empty questions to 400. The embedding provider is a
module-level seam for tests, like ``uow``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ai_providers.contracts import EmbeddingProvider, ProviderError
from ai_providers.ollama import OllamaEmbeddingProvider
from apps.api.routes.auth import uow
from apps.api.routes.sources import catalog_context
from apps.api.routes.users import ready_user
from foundation.config import ModelProfile, Settings
from identity_access.auth import AuthenticatedUser
from retrieval_answering.retrieval import EmptyQueryError, retrieve

router = APIRouter(prefix="/api")

_embeddings: EmbeddingProvider | None = None
_profile: ModelProfile | None = None


def embeddings() -> EmbeddingProvider:
    global _embeddings
    if _embeddings is None:
        settings = Settings.load()
        profile = settings.active_profile
        _embeddings = OllamaEmbeddingProvider(
            settings.ollama_host, profile.embedding_model, profile.embedding_dimensions
        )
    return _embeddings


def profile() -> ModelProfile:
    global _profile
    if _profile is None:
        _profile = Settings.load().active_profile
    return _profile


class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    source_id: str | None = None


@router.post("/search")
async def search(
    body: SearchRequest,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    try:
        ranked = await retrieve(
            uow(), embeddings(),
            context=await catalog_context(user),
            query=body.query,
            profile=profile(),
            limit=max(1, min(body.limit, 50)),
            source_id=body.source_id,
        )
    except EmptyQueryError:
        raise HTTPException(status_code=400, detail="query is empty") from None
    except ProviderError as failure:
        raise HTTPException(
            status_code=503,
            detail=f"embedding provider unavailable ({failure.reason}); "
                   "check the model host and retry",
        ) from None
    return {
        "query_language": ranked.query_language,
        "confident": ranked.is_confident,
        "items": ranked.to_json(),
    }
