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

from agent.assistant_conversations import (
    AssistantConversations,
    ConversationRow,
    MessageRow,
)
from agent.assistant_feedback import AssistantFeedback
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


class BulkDeleteIn(BaseModel):
    """Body cho POST /conversations/bulk-delete (R2, ADR round2). Cap 100 id/request —
    quá tay ⇒ 422 Pydantic (không 400 thủ công vì Pydantic đủ). Client vượt cap chia
    batch, endpoint idempotent nên không rủi ro dup delete."""

    model_config = ConfigDict(extra="ignore")

    ids: list[int] = Field(min_length=1, max_length=100)


class BulkDeleteOut(BaseModel):
    """Response `{deleted, skipped}`. `deleted` = id thực sự UPDATE (đổi trạng thái);
    `skipped` = id không match (không tồn tại / cross-user / đã xoá trước). Không phân
    biệt lý do skip — cùng bài 404 single-delete (không leak existence)."""

    deleted: list[int]
    skipped: list[int]


class MemoryOut(BaseModel):
    memory_id: int
    content: str
    created_at: datetime


class MemoryListOut(BaseModel):
    items: list[MemoryOut]
    next_cursor: datetime | None


class FeedbackIn(BaseModel):
    """Body cho POST /messages/{id}/feedback (R4). `rating` chỉ ±1 — thumbs up/down
    binary, không có thang 5-sao (v1). `note` optional, max 2000 char (đủ 1 đoạn góp ý,
    ngăn free-form dài quá). CHECK ở DB (a14) là belt-and-braces cho path bypass endpoint."""

    model_config = ConfigDict(extra="ignore")

    rating: int = Field(description="±1 · thumbs up/down")
    note: str | None = Field(default=None, max_length=2000)


class FeedbackOut(BaseModel):
    """Response upsert feedback. `updated_at` echo cho client verify write; không trả
    `rating` (client đã có), không trả `note` (redundant)."""

    message_id: int
    updated_at: datetime


class MessageOut(BaseModel):
    """1 message trong hội thoại (P2.4d chat persistence). `role` ∈ {user, assistant,
    system} — string thay vì Literal để tránh Pydantic reject nếu ENUM DB sau này mở
    rộng (msg_role có `system` sẵn ở ENUM nhưng chat endpoint chưa dùng)."""

    message_id: int
    role: str
    content: str
    created_at: datetime


class MessageListOut(BaseModel):
    """List messages trong 1 hội thoại. `next_cursor` = `created_at` của item cuối
    page — client truyền lại ở `before` để load thêm cũ hơn. ASC order (khớp reading
    order), khác conversations list DESC (recent first)."""

    items: list[MessageOut]
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


def _message_to_out(row: MessageRow) -> MessageOut:
    return MessageOut(
        message_id=row.message_id,
        role=row.role,
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

    @router.post(
        "/conversations/bulk-delete",
        response_model=BulkDeleteOut,
    )
    async def bulk_delete_conversations(
        payload: BulkDeleteIn,
        identity: UserIdentity = Depends(user_identity_from_cookie),
    ) -> BulkDeleteOut:
        """R2 · xoá nhiều hội thoại 1 request. Dedup input trước khi gọi repo (client
        gửi trùng ⇒ 1 lần UPDATE, không lỡ đếm vào skipped)."""
        unique_ids = list(dict.fromkeys(payload.ids))
        repo = AssistantConversations(session_factory, user_scope=identity.user_id)
        deleted = await repo.bulk_soft_delete(unique_ids)
        deleted_set = set(deleted)
        skipped = [i for i in unique_ids if i not in deleted_set]
        return BulkDeleteOut(deleted=deleted, skipped=skipped)

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

    @router.get(
        "/conversations/{conversation_id}/messages",
        response_model=MessageListOut,
    )
    async def list_conversation_messages(
        conversation_id: int,
        identity: Annotated[UserIdentity, Depends(user_identity_from_cookie)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before: Annotated[datetime | None, Query()] = None,
    ) -> MessageListOut:
        """List messages trong 1 hội thoại (P2.4d chat persistence).

        **Ownership check TRƯỚC list** (`get()` first). Cross-user hoặc soft-deleted
        conversation ⇒ 404 `conversation_not_found` (không leak existence). Repo
        `list_messages` cũng cover `user_id = user_scope` — belt-and-braces, nếu ai
        skip check `get()` thì rowcount vẫn = 0.
        """
        repo = AssistantConversations(session_factory, user_scope=identity.user_id)
        if await repo.get(conversation_id) is None:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        rows = await repo.list_messages(conversation_id, limit=limit, before_created_at=before)
        # ASC — next_cursor = created_at của item CUỐI page (mới nhất trong page).
        # Client truyền lại ở `before` để load thêm cũ hơn — nhưng semantic ASC + before
        # = trước cursor này về thời gian, tức trả về những cái CŨ hơn. Endpoint này
        # UX pattern: "scroll up để load lịch sử cũ hơn" ngược với chat UI thông thường.
        # Nếu muốn "scroll down load mới hơn" thì flip logic ở client (sort DESC UI).
        next_cursor = rows[-1].created_at if len(rows) == limit else None
        return MessageListOut(
            items=[_message_to_out(r) for r in rows],
            next_cursor=next_cursor,
        )

    # ── Feedback (R4 · ADR round2) ───────────────────────────────────────────────

    @router.post(
        "/messages/{message_id}/feedback",
        response_model=FeedbackOut,
    )
    async def upsert_message_feedback(
        message_id: int,
        payload: FeedbackIn,
        identity: UserIdentity = Depends(user_identity_from_cookie),
    ) -> FeedbackOut:
        """Upsert thumbs up/down. 422 nếu rating không ∈ {-1, 1}. 404 nếu message không
        tồn tại / cross-user / conversation đã xoá / role != 'assistant'. Không phân biệt
        lý do 404 (cùng bài CRUD — không leak existence).

        `note` KHÔNG cast lên Langfuse (I16 analog); telemetry log rating + trace_id
        để aggregate ở tools observability.
        """
        if payload.rating not in (-1, 1):
            raise HTTPException(status_code=422, detail="rating_must_be_plus_or_minus_one")
        repo = AssistantFeedback(session_factory, user_scope=identity.user_id)
        updated = await repo.upsert(message_id, payload.rating, payload.note)  # type: ignore[arg-type]
        if updated is None:
            raise HTTPException(status_code=404, detail="message_not_found")
        return FeedbackOut(message_id=message_id, updated_at=updated)

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
