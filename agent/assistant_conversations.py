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

from db.models import AssistantConversation, AssistantMessage

_LIST_LIMIT_MAX = 100
# Messages endpoint list dùng cap cao hơn conversations (200 vs 100) — 1 hội thoại có
# thể dài hàng trăm turn; UI thường load 50 rồi scroll ngược.
_MESSAGES_LIMIT_MAX = 200


@dataclass(frozen=True)
class ConversationRow:
    conversation_id: int
    title: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class MessageRow:
    """1 tin nhắn trong hội thoại. `role` ∈ {"user", "assistant", "system"} — ENUM
    `assistant.msg_role` ở DB, không convert enum type ở Python (giữ str để endpoint
    trả JSON không cần custom serializer)."""

    message_id: int
    role: str
    content: str
    created_at: datetime


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

    # ── Messages (P2.4d chat persistence) ───────────────────────────────────────

    async def append_pair(
        self,
        conversation_id: int,
        *,
        user_content: str,
        assistant_content: str,
    ) -> tuple[int, int]:
        """1 transaction: INSERT user + INSERT assistant + UPDATE conversations.updated_at.

        Trả (user_message_id, assistant_message_id). Cross-user hoặc soft-deleted
        conversation ⇒ endpoint layer PHẢI check `get()` trước — repo không recheck
        (tin caller). FK CASCADE + `user_id NOT NULL` ở message row đảm bảo scope
        integrity ở DB layer.

        Fail giữa transaction ⇒ ROLLBACK toàn bộ: KHÔNG có case "user message không có
        assistant reply" trong DB — history luôn consistent theo cặp.

        Bump `updated_at` để hội thoại nhảy lên đầu list-recent — UX pattern "most
        recently active first" (Slack/Messenger cùng bài). Không bump ở P2.4c CRUD
        `list_recent` sort đúng logic này rồi.
        """
        if not user_content or not user_content.strip():
            raise ValueError("user_content must be non-empty")
        if not assistant_content or not assistant_content.strip():
            raise ValueError("assistant_content must be non-empty")
        from sqlalchemy import func

        user_stmt = (
            insert(AssistantMessage)
            .values(
                conversation_id=conversation_id,
                user_id=self._user_scope,
                role="user",
                content=user_content,
            )
            .returning(AssistantMessage.message_id)
        )
        assistant_stmt = (
            insert(AssistantMessage)
            .values(
                conversation_id=conversation_id,
                user_id=self._user_scope,
                role="assistant",
                content=assistant_content,
            )
            .returning(AssistantMessage.message_id)
        )
        bump_stmt = (
            update(AssistantConversation)
            .where(
                AssistantConversation.conversation_id == conversation_id,
                AssistantConversation.user_id == self._user_scope,
            )
            .values(updated_at=func.now())
        )
        async with self._sm() as session:
            user_mid = (await session.execute(user_stmt)).scalar_one()
            assistant_mid = (await session.execute(assistant_stmt)).scalar_one()
            await session.execute(bump_stmt)
            await session.commit()
        return int(user_mid), int(assistant_mid)

    async def list_messages(
        self,
        conversation_id: int,
        limit: int,
        before_created_at: datetime | None = None,
    ) -> list[MessageRow]:
        """List message trong 1 hội thoại, ASC by created_at (đọc từ đầu hội thoại
        khớp thứ tự thời gian — KHÁC list_recent conversations là DESC).

        `limit` clamp [1, 200]. Cursor `before_created_at` — khi ASC thì cursor semantic
        là "trước cursor này" theo thời gian. Trong context chat UI thường load full từ
        đầu hoặc load thêm cũ hơn khi scroll lên, nên ASC hợp hơn DESC.

        Ownership check ở endpoint layer (`get()` trước). Repo không recheck — nếu
        caller không check, cross-user rowcount vẫn = 0 nhờ `user_id = user_scope`
        redundant filter (belt-and-braces).
        """
        clamped = max(1, min(limit, _MESSAGES_LIMIT_MAX))
        stmt = select(
            AssistantMessage.message_id,
            AssistantMessage.role,
            AssistantMessage.content,
            AssistantMessage.created_at,
        ).where(
            AssistantMessage.conversation_id == conversation_id,
            AssistantMessage.user_id == self._user_scope,
        )
        if before_created_at is not None:
            stmt = stmt.where(AssistantMessage.created_at < before_created_at)
        # Tie-breaker `message_id`: 2 message trong cùng append_pair transaction có
        # `created_at = now()` giống nhau (server default eval 1 lần cho cả tx trên
        # nhiều Postgres config). Không tie-break ⇒ order undefined giữa user/assistant
        # cùng cặp; chat UI hiển thị lộn ngược. `message_id` là IDENTITY monotonic ⇒
        # đảm bảo user < assistant (INSERT trước) khi timestamp tie.
        stmt = stmt.order_by(AssistantMessage.created_at, AssistantMessage.message_id).limit(
            clamped
        )
        async with self._sm() as session:
            rows = (await session.execute(stmt)).all()
        return [
            MessageRow(
                message_id=int(r[0]),
                role=r[1],
                content=r[2],
                created_at=r[3],
            )
            for r in rows
        ]


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
