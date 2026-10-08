"""Conversation routes (Task 15c).

List/reopen/rename/delete are scoped to the signed-in user's own threads;
unknown and foreign ids return the identical generic 404. Reopening returns
stored citations; the UI re-authorizes a citation's source against the
catalog when the evidence panel opens.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from apps.api.routes.auth import uow
from apps.api.routes.sources import catalog_context
from apps.api.routes.users import ready_user
from identity_access.auth import AuthenticatedUser
from retrieval_answering.conversation import (
    ConversationNotFound,
    delete_conversation,
    get_conversation,
    list_conversations,
    rename_conversation,
)

router = APIRouter(prefix="/api")


class RenameRequest(BaseModel):
    title: str


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="no such conversation")


@router.get("/conversations")
async def conversations(
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> list[dict]:
    context = await catalog_context(user)
    return await list_conversations(uow(), context=context)


@router.get("/conversations/{conversation_id}")
async def conversation(
    conversation_id: str,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    context = await catalog_context(user)
    try:
        return await get_conversation(uow(), context=context, conversation_id=conversation_id)
    except ConversationNotFound:
        raise _not_found() from None


@router.put("/conversations/{conversation_id}", status_code=204)
async def rename(
    conversation_id: str,
    body: RenameRequest,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> Response:
    context = await catalog_context(user)
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is empty")
    try:
        await rename_conversation(
            uow(), context=context, conversation_id=conversation_id, title=title
        )
    except ConversationNotFound:
        raise _not_found() from None
    return Response(status_code=204)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def remove(
    conversation_id: str,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> Response:
    context = await catalog_context(user)
    try:
        await delete_conversation(uow(), context=context, conversation_id=conversation_id)
    except ConversationNotFound:
        raise _not_found() from None
    return Response(status_code=204)
