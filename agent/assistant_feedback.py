"""Feedback repo cho R4 (ADR round2) — thumbs up/down trên `assistant.messages`.

Upsert per `(message_id, user_id)`: 1 user rate 1 message = 1 row, sửa rating = UPDATE
in-place. Ownership check bằng JOIN với `messages` + `conversations` — repo tự lo, endpoint
KHÔNG check trước để tránh race giữa "check owned" và "upsert" (2 câu tách gãy khi conv
bị bulk-delete xen giữa).

**Feedback CHỈ trên `role='assistant'` message.** User rate tin nhắn của chính mình = vô
nghĩa; enforce ở SQL WHERE (không dựa endpoint). Repo trả `None` nếu không match — endpoint
map thành 404.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Rating = Literal[-1, 1]


class AssistantFeedback:
    """CRUD feedback per-user. Cùng discipline `AssistantConversations`: instance scope 1
    user, không API nào nhận `user_id` argument."""

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

    async def upsert(
        self,
        message_id: int,
        rating: Rating,
        note: str | None,
    ) -> datetime | None:
        """Upsert feedback. Trả `updated_at` nếu thành công, `None` nếu:
        - message không tồn tại
        - message thuộc conversation của user KHÁC
        - conversation đã soft-delete
        - message role != 'assistant' (rating user message vô nghĩa)

        1 câu (WITH … INSERT … RETURNING): atomic — không có window "check rồi upsert" cho
        bulk-delete xen. Nếu conversation bị soft-delete cùng lúc, CTE trả 0 row ⇒ INSERT
        không chạy ⇒ RETURNING rỗng ⇒ trả None.

        `note` optional — free text từ user, KHÔNG cast lên Langfuse (I16 analog: Langfuse
        chỉ nhận Scrubbed; note chứa PII risk cao vì user viết tự do). Note đọc bằng
        SQL trực tiếp khi cần debug thủ công.
        """
        stmt = text(
            """
            WITH owned_msg AS (
                SELECT m.message_id
                FROM assistant.messages m
                JOIN assistant.conversations c
                  ON c.conversation_id = m.conversation_id
                WHERE m.message_id = :message_id
                  AND c.user_id = :user_id
                  AND m.user_id = :user_id
                  AND c.deleted_at IS NULL
                  AND m.role = 'assistant'
            )
            INSERT INTO assistant.message_feedback
                (message_id, user_id, rating, note, created_at, updated_at)
            SELECT :message_id, :user_id, :rating, :note, now(), now()
            FROM owned_msg
            ON CONFLICT (message_id, user_id) DO UPDATE
              SET rating = EXCLUDED.rating,
                  note = EXCLUDED.note,
                  updated_at = now()
            RETURNING updated_at
            """
        ).bindparams(
            bindparam("message_id"),
            bindparam("user_id"),
            bindparam("rating"),
            bindparam("note"),
        )
        async with self._sm() as session:
            row = (
                await session.execute(
                    stmt,
                    {
                        "message_id": message_id,
                        "user_id": self._user_scope,
                        "rating": rating,
                        "note": note,
                    },
                )
            ).first()
            await session.commit()
        return row[0] if row is not None else None
