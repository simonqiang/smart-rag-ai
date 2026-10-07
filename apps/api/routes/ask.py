"""Ask route (Task 14c).

One pipeline — rewrite (optional) → authorized retrieval → grounded answer —
delivered as JSON or typed SSE frames (``Accept: text/event-stream``).
Persistence is completion-gated: only fully validated answers record their
exchange, so a cancelled or failed stream leaves no half conversation.
Resume replays the stored answer for a request id; unknown or foreign
requests get a typed ``unknown_request`` error frame. Audit metadata stays
count-and-identifier only.

Seams (``_generation``, ``_embeddings``, ``_profile``, ``_rewriter``) are
module-level for tests, like ``uow``. ``cancelled`` frames: a client abort
closes the SSE body, which cancels generation at the next chunk boundary —
the client renders its own cancelled state (spec §15 streaming contract).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ai_providers.contracts import EmbeddingProvider, GenerationProvider
from ai_providers.ollama import OllamaEmbeddingProvider, OllamaGenerationProvider
from apps.api.routes.auth import uow
from apps.api.routes.sources import catalog_context
from apps.api.routes.users import ready_user
from foundation.config import ModelProfile, Settings
from foundation.events import EventWriter
from identity_access.auth import AuthenticatedUser
from retrieval_answering.answering import (
    DEFAULT_ERROR_MESSAGE,
    SAFE_ERROR_MESSAGES,
    AnswerStreamEvent,
    QueryRewriter,
    answer,
    stream_answer,
)
from retrieval_answering.citations import Citation, CitationError
from retrieval_answering.conversation import (
    ConversationContext,
    ConversationNotFound,
    create_conversation,
    find_completed_answer,
    load_context,
    record_exchange,
)
from retrieval_answering.retrieval import retrieve

router = APIRouter(prefix="/api")

# Task 23 flips this when a hosted GLM profile is consented to.
PROVIDER_BADGE = {"name": "ollama", "local": True}

_generation: GenerationProvider | None = None
_embeddings: EmbeddingProvider | None = None
_profile: ModelProfile | None = None
_rewriter: QueryRewriter | None = None


def generation() -> GenerationProvider:
    global _generation
    if _generation is None:
        settings = Settings.load()
        _generation = OllamaGenerationProvider(settings.ollama_host, profile().chat_model)
    return _generation


def embeddings() -> EmbeddingProvider:
    global _embeddings
    if _embeddings is None:
        settings = Settings.load()
        _embeddings = OllamaEmbeddingProvider(
            settings.ollama_host, profile().embedding_model, profile().embedding_dimensions
        )
    return _embeddings


def profile() -> ModelProfile:
    global _profile
    if _profile is None:
        _profile = Settings.load().active_profile
    return _profile


def rewriter() -> QueryRewriter | None:
    return _rewriter


class AskRequest(BaseModel):
    question: str
    conversation_id: str | None = None
    source_id: str | None = None


def _sse(event: AnswerStreamEvent) -> str:
    return f"data: {json.dumps(event.to_json(), ensure_ascii=False)}\n\n"


def _safe_error_message(reason: str | None) -> str:
    return SAFE_ERROR_MESSAGES.get(reason or "", DEFAULT_ERROR_MESSAGE)


@router.post("/ask")
async def ask(
    body: AskRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> object:
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is empty")
    context = await catalog_context(user)

    if body.conversation_id:
        try:
            history = await load_context(
                uow(), context=context, conversation_id=body.conversation_id
            )
        except ConversationNotFound:
            raise HTTPException(
                status_code=404, detail="no such conversation"
            ) from None
        conversation_id = body.conversation_id
    else:
        conversation_id = await create_conversation(
            uow(), context, title=question[:80]
        )
        history = ConversationContext()

    query = _rewriter.rewrite(question, history) if _rewriter else question
    ranked = await retrieve(
        uow(), embeddings(), context=context, query=query,
        profile=profile(), source_id=body.source_id,
    )
    request_id = uuid.uuid4().hex

    if "text/event-stream" in request.headers.get("accept", ""):
        return StreamingResponse(
            _streaming_exchange(
                question=question,
                ranked=ranked,
                history=history,
                context=context,
                conversation_id=conversation_id,
                request_id=request_id,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    try:
        result = answer(
            question, ranked, generation=generation(), history=history,
            request_id=request_id,
        )
    except CitationError as failure:
        raise HTTPException(
            status_code=502, detail=_safe_error_message(failure.reason)
        ) from None
    await record_exchange(
        uow(), EventWriter(), context=context, conversation_id=conversation_id,
        question=question, answer_text=result.answer_text,
        citations=[citation.to_json() for citation in result.citations],
        language=result.language, request_id=request_id,
        insufficient_evidence=result.insufficient_evidence,
    )
    return {
        "request_id": request_id,
        "conversation_id": conversation_id,
        "question_language": result.language,
        "answer": {
            "text": result.answer_text,
            "citations": [citation.to_json() for citation in result.citations],
        },
        "insufficient_evidence": result.insufficient_evidence,
        "provider": PROVIDER_BADGE,
    }


async def _streaming_exchange(
    *,
    question: str,
    ranked,
    history: ConversationContext,
    context,
    conversation_id: str,
    request_id: str,
):
    events = stream_answer(
        question, ranked, generation=generation(), history=history, request_id=request_id
    )
    language = "en"
    citations: list[Citation] = []
    completed: AnswerStreamEvent | None = None
    try:
        async for event in events:
            if event.kind == "started" and event.language:
                language = event.language
            if event.kind == "citation" and event.citation:
                citations.append(event.citation)
            if event.kind == "completed":
                completed = event
            yield _sse(event)
    finally:
        if completed is not None:
            await record_exchange(
                uow(), EventWriter(), context=context, conversation_id=conversation_id,
                question=question, answer_text=completed.text or "",
                citations=[citation.to_json() for citation in citations],
                language=language, request_id=request_id,
                insufficient_evidence=completed.insufficient_evidence is True,
            )


@router.get("/ask/{request_id}/events")
async def resume(
    request_id: str,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> StreamingResponse:
    context = await catalog_context(user)
    stored = await find_completed_answer(uow(), context=context, request_id=request_id)

    async def replay() -> AsyncIterator[str]:
        seq = 0

        def frame(**fields) -> AnswerStreamEvent:
            nonlocal seq
            seq += 1
            return AnswerStreamEvent(request_id=request_id, seq=seq, **fields)

        if stored is None:
            yield _sse(frame(kind="error", reason="unknown_request",
                             message=_safe_error_message("unknown_request")))
            return
        yield _sse(frame(kind="started", language=stored["language"] or "en"))
        yield _sse(frame(kind="delta", text=stored["content"]))
        for citation in stored["citations"] or []:
            yield _sse(frame(kind="citation", citation=Citation(**citation)))
        yield _sse(frame(kind="completed", text=stored["content"],
                         insufficient_evidence=stored["insufficient"]))

    return StreamingResponse(
        replay(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )
