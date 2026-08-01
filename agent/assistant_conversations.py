"""Conversations CRUD repo (Tầng 2 · Phase 2.4c).

Sit cùng tier `agent.assistant_*` (I1c luồng A). Repo cho `assistant.conversations`:
create / list / get / update_title / soft_delete. Đối xứng với `AssistantMemory` — cùng
constructor pattern (`user_scope` bắt buộc), cùng discipline WHERE user_id đứng trước
ORDER/LIMIT (U-scope hard filter — hai lỗi mới leak được cross-user).

**Soft-delete** (không hard). Comment trong `db/models.py::AssistantConversation` đã ghim
lựa chọn này ở P2.1: `deleted_at IS NULL` = row sống. Partial index
`idx_assistant_conv_user_updated WHERE deleted_at IS NULL` cover query `list_recent`.
Hard delete để tương lai (khôi phục UI + `messages` CASCADE trong FK khiến hard delete
mất luôn history).

**Cursor pagination** (không offset). `before_updated_at` — page-drift-safe khi conversation
mới xen kẽ. Client giữ `next_cursor` = `updated_at` của item cuối cùng page trước.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import AssistantConversation

_LIST_LIMIT_MAX = 100


@dataclass(frozen=True)
class ConversationRow:
    conversation_id: int
    title: str | None
    created_at: datetime
    updated_at: datetime


class AssistantConversations:
    """CRUD hội thoại per-user. Instance chỉ hoạt động trong scope 1 user — không có API
    nào nhận `user_id` như argument, ngăn caller passthrough sai scope."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        user_scope: str,
    ) -> None:
        if not user_scope:
            raise ValueError("user_scope is required — no default, no cross-user surface")
        self._sm = session_factory
        self._user_scope = user_scope

    async def create(self, title: str | None) -> ConversationRow:
        """Tạo hội thoại mới. `title` optional (user có thể đặt tên sau qua PATCH).

        Empty title (`""`) ⇒ NULL (đối xử như "chưa đặt tên") — không lưu row với title
        rỗng vì UI hiển thị vô nghĩa và filter WHERE title != '' phải viết ở mọi list.
        """
        stmt = (
            insert(AssistantConversation)
            .values(user_id=self._user_scope, title=title or None)
            .returning(
                AssistantConversation.conversation_id,
                AssistantConversation.title,
                AssistantConversation.created_at,
                AssistantConversation.updated_at,
            )
        )
        async with self._sm() as session:
            row = (await session.execute(stmt)).one()
            await session.commit()
        return ConversationRow(
            conversation_id=int(row[0]),
            title=row[1],
            created_at=row[2],
            updated_at=row[3],
        )

    async def list_recent(
        self,
        limit: int,
        before_updated_at: datetime | None = None,
    ) -> list[ConversationRow]:
        """List hội thoại chưa xoá, sắp xếp updated_at DESC. Cursor pagination.

        `limit` clamp [1, 100] — client gửi ngoài range không raise, chỉ clamp (UX-friendly).
        `before_updated_at` None ⇒ page đầu. Non-None ⇒ WHERE updated_at < cursor (strict).
        Partial index `idx_assistant_conv_user_updated` cover được query này.
        """
        clamped = max(1, min(limit, _LIST_LIMIT_MAX))
        stmt = select(
            AssistantConversation.conversation_id,
            AssistantConversation.title,
            AssistantConversation.created_at,
            AssistantConversation.updated_at,
        ).where(
            AssistantConversation.user_id == self._user_scope,
            AssistantConversation.deleted_at.is_(None),
        )
        if before_updated_at is not None:
            stmt = stmt.where(AssistantConversation.updated_at < before_updated_at)
        stmt = stmt.order_by(AssistantConversation.updated_at.desc()).limit(clamped)
        async with self._sm() as session:
            rows = (await session.execute(stmt)).all()
        return [
            ConversationRow(
                conversation_id=int(r[0]),
                title=r[1],
                created_at=r[2],
                updated_at=r[3],
            )
            for r in rows
        ]

    async def get(self, conversation_id: int) -> ConversationRow | None:
        """Get 1 hội thoại. Trả None khi: (a) không tồn tại, (b) thuộc user khác, (c) đã
        soft-delete. Cả 3 case đều 404 ở endpoint — cố ý không phân biệt (leak existence).
        """
        stmt = (
            select(
                AssistantConversation.conversation_id,
                AssistantConversation.title,
                AssistantConversation.created_at,
                AssistantConversation.updated_at,
            )
            .where(
                AssistantConversation.conversation_id == conversation_id,
                AssistantConversation.user_id == self._user_scope,
                AssistantConversation.deleted_at.is_(None),
            )
            .limit(1)
        )
        async with self._sm() as session:
            row = (await session.execute(stmt)).first()
        if row is None:
            return None
        return ConversationRow(
            conversation_id=int(row[0]),
            title=row[1],
            created_at=row[2],
            updated_at=row[3],
        )

    async def update_title(self, conversation_id: int, title: str) -> bool:
        """Đổi tên. Trả False khi 404 (giống `get`). Bump `updated_at = now()` cùng câu
        UPDATE để item nhảy lên đầu list (UX pattern: edited → most recent)."""
        if not title or not title.strip():
            raise ValueError("title must be non-empty")
        from sqlalchemy import func

        stmt = (
            update(AssistantConversation)
            .where(
                AssistantConversation.conversation_id == conversation_id,
                AssistantConversation.user_id == self._user_scope,
                AssistantConversation.deleted_at.is_(None),
            )
            .values(title=title, updated_at=func.now())
        )
        async with self._sm() as session:
            result = cast("CursorResult[Any]", await session.execute(stmt))
            await session.commit()
        return (result.rowcount or 0) > 0

    async def soft_delete(self, conversation_id: int) -> bool:
        """Set `deleted_at = now()`. Idempotent-ish: xoá lại ⇒ False (đã có deleted_at
        không NULL, WHERE `deleted_at IS NULL` không match). Client thấy 404 lần hai."""
        from sqlalchemy import func

        stmt = (
            update(AssistantConversation)
            .where(
                AssistantConversation.conversation_id == conversation_id,
                AssistantConversation.user_id == self._user_scope,
                AssistantConversation.deleted_at.is_(None),
            )
            .values(deleted_at=func.now())
        )
        async with self._sm() as session:
            result = cast("CursorResult[Any]", await session.execute(stmt))
            await session.commit()
        return (result.rowcount or 0) > 0


async def _debug_purge_for_user(
    session_factory: async_sessionmaker[AsyncSession], user_scope: str
) -> None:
    """TEST-ONLY hard delete mọi conversation cho user. KHÔNG expose qua endpoint (không
    có route gọi nó). Dùng ở test cleanup để tránh row rớt lại giữa test."""
    async with session_factory() as session:
        await session.execute(
            delete(AssistantConversation).where(AssistantConversation.user_id == user_scope)
        )
        await session.commit()
