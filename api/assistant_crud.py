"""Assistant CRUD router — Tầng 2 endpoints cho conversations + memories (Phase 2.4c).

Consume 2 repo primitive (P2.4c) + `user_identity_from_cookie` (P2.4a). KHÔNG chạm LLM,
KHÔNG đốt token, KHÔNG cần tier gate (rate/cost chỉ áp `/chat` vì đó là nơi tiêu tiền).

**Ranh giới HTTP status** (chốt ở brief §"Bất biến chạm"):
- 401 · missing / invalid cookie (từ `user_identity_from_cookie`).
- 404 · resource không tồn tại HOẶC thuộc user khác HOẶC đã soft-delete. Ba case trả cùng
  một shape để KHÔNG leak sự tồn tại của resource người khác — attacker enum id không phân
  biệt được "id có nhưng không phải của bạn" với "id không có".
- 422 · body Pydantic invalid (title empty, quá dài, v.v.).
- 200 · get/list/patch OK; 201 · create OK; 204 · delete OK (No Content).

**Cursor pagination**: `before` là ISO datetime lấy từ item cuối cùng page trước
(`updated_at` cho conversations, `created_at` cho memories). Contract cùng shape hai
endpoint để UI 1 helper client dùng chung.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.assistant_conversations import AssistantConversations, ConversationRow
from agent.assistant_memory import AssistantMemory, MemoryRow
from agent.embedder import Embedder, default_embedder
from auth.user_identity import UserIdentity, user_identity_from_cookie


class ConversationCreateIn(BaseModel):
    """Body cho POST /conversations. `title` optional — user có thể tạo trước, đặt tên
    sau qua PATCH (UI: 'New chat' rồi tự sinh từ câu đầu, hoặc để trống)."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = Field(default=None, max_length=200)


class ConversationPatchIn(BaseModel):
    """Body cho PATCH /conversations/{id}. `title` REQUIRED — endpoint duy nhất
    ở P2.4c cho phép sửa (soft-delete có route riêng)."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)


class ConversationOut(BaseModel):
    conversation_id: int
    title: str | None
    created_at: datetime
    updated_at: datetime


class ConversationListOut(BaseModel):
    """`next_cursor` = `updated_at` của item cuối page hiện tại. None ⇒ hết trang.

    Client gửi lại cursor này ở `before` để load page tiếp — cùng bài stripe/cursor. Không
    dùng offset để tránh page-drift khi có conversation mới tạo giữa 2 request."""

    items: list[ConversationOut]
    next_cursor: datetime | None


class MemoryOut(BaseModel):
    memory_id: int
    content: str
    created_at: datetime


class MemoryListOut(BaseModel):
    items: list[MemoryOut]
    next_cursor: datetime | None


def _conversation_to_out(row: ConversationRow) -> ConversationOut:
    return ConversationOut(
        conversation_id=row.conversation_id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _memory_to_out(row: MemoryRow) -> MemoryOut:
    return MemoryOut(
        memory_id=row.memory_id,
        content=row.content,
        created_at=row.created_at,
    )


def build_router(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    embedder_factory: Callable[[], Embedder] = default_embedder,
) -> APIRouter:
    """Router factory. `session_factory` share với `assistant_chat` (cùng svc_ohana_ai).
    `embedder_factory` inject-able để test — CRUD KHÔNG cần embed thực (không recall),
    nhưng `AssistantMemory` constructor yêu cầu embedder object (I11 discipline: 1 instance
    memory = 1 embedder pinned, không mix path).
    """
    router = APIRouter(prefix="/assistant", tags=["assistant"])

    # ── Conversations ────────────────────────────────────────────────────────────

    @router.post(
        "/conversations",
        response_model=ConversationOut,
        status_code=201,
    )
    async def create_conversation(
        payload: ConversationCreateIn,
        identity: UserIdentity = Depends(user_identity_from_cookie),
    ) -> ConversationOut:
        repo = AssistantConversations(session_factory, user_scope=identity.user_id)
        row = await repo.create(title=payload.title)
        return _conversation_to_out(row)

    @router.get(
        "/conversations",
        response_model=ConversationListOut,
    )
    async def list_conversations(
        identity: Annotated[UserIdentity, Depends(user_identity_from_cookie)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        before: Annotated[datetime | None, Query()] = None,
    ) -> ConversationListOut:
        repo = AssistantConversations(session_factory, user_scope=identity.user_id)
        rows = await repo.list_recent(limit=limit, before_updated_at=before)
        next_cursor = rows[-1].updated_at if len(rows) == limit else None
        return ConversationListOut(
            items=[_conversation_to_out(r) for r in rows],
            next_cursor=next_cursor,
        )

    @router.get(
        "/conversations/{conversation_id}",
        response_model=ConversationOut,
    )
    async def get_conversation(
        conversation_id: int,
        identity: UserIdentity = Depends(user_identity_from_cookie),
    ) -> ConversationOut:
        repo = AssistantConversations(session_factory, user_scope=identity.user_id)
        row = await repo.get(conversation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        return _conversation_to_out(row)

    @router.patch(
        "/conversations/{conversation_id}",
        response_model=ConversationOut,
    )
    async def patch_conversation(
        conversation_id: int,
        payload: ConversationPatchIn,
        identity: UserIdentity = Depends(user_identity_from_cookie),
    ) -> ConversationOut:
        repo = AssistantConversations(session_factory, user_scope=identity.user_id)
        ok = await repo.update_title(conversation_id, payload.title)
        if not ok:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        # Refetch để trả row cập nhật (updated_at + title mới) — 1 round-trip trả lời
        # đầy đủ hơn là chỉ trả HTTP 204 (UI khỏi phải GET lại).
        row = await repo.get(conversation_id)
        if row is None:
            # Race hiếm: xoá xen giữa update và get. Không giả 200 — 404 phản ánh state
            # cuối cùng client nhìn thấy.
            raise HTTPException(status_code=404, detail="conversation_not_found")
        return _conversation_to_out(row)

    @router.delete(
        "/conversations/{conversation_id}",
        status_code=204,
        response_class=Response,
    )
    async def delete_conversation(
        conversation_id: int,
        identity: UserIdentity = Depends(user_identity_from_cookie),
    ) -> Response:
        repo = AssistantConversations(session_factory, user_scope=identity.user_id)
        ok = await repo.soft_delete(conversation_id)
        if not ok:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        return Response(status_code=204)

    # ── Memories ─────────────────────────────────────────────────────────────────

    @router.get(
        "/memories",
        response_model=MemoryListOut,
    )
    async def list_memories(
        identity: Annotated[UserIdentity, Depends(user_identity_from_cookie)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        before: Annotated[datetime | None, Query()] = None,
    ) -> MemoryListOut:
        embedder: Embedder = embedder_factory()
        memory = AssistantMemory(session_factory, embedder, user_scope=identity.user_id)
        rows = await memory.list_memories(limit=limit, before_created_at=before)
        next_cursor = rows[-1].created_at if len(rows) == limit else None
        return MemoryListOut(
            items=[_memory_to_out(r) for r in rows],
            next_cursor=next_cursor,
        )

    @router.delete(
        "/memories/{memory_id}",
        status_code=204,
        response_class=Response,
    )
    async def delete_memory(
        memory_id: int,
        identity: UserIdentity = Depends(user_identity_from_cookie),
    ) -> Response:
        embedder: Embedder = embedder_factory()
        memory = AssistantMemory(session_factory, embedder, user_scope=identity.user_id)
        ok = await memory.delete_memory(memory_id)
        if not ok:
            raise HTTPException(status_code=404, detail="memory_not_found")
        return Response(status_code=204)

    return router
