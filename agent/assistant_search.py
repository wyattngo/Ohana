"""Search repo cho R3 (ADR round2) — FTS trên messages + ILIKE trên conversation titles.

Đối xứng CRUD: instance scope 1 user (`user_scope` bắt buộc). WHERE `user_id = :u` là hard
filter — hai lỗi mới leak cross-user.

**FTS 'simple' cho content** — index a15. `plainto_tsquery('simple', :q)` handle escape
input user (không cần sanitize thủ công — Postgres tokenize + escape). `ts_rank_cd` cho
scoring; `ts_headline` cho snippet có `<em>...</em>` highlight.

**ILIKE cho title** — bảng nhỏ (< 100 conv/user), không cần index. `lower(title) LIKE
lower('%q%')` case-insensitive; escape `%` và `_` trong q trước khi bind (LIKE metachar).

**Order: message DESC by created_at, title DESC by updated_at.** V1 không sort theo rank
— user thường search "hội thoại gần đây bàn X", recency > relevance. Sort rank nếu về
sau đo được cần.

**Không pagination v1.** Trả cap tối đa 20/loại (40 total). Query đầy trả 20 ⇒ client
biết còn nữa (hiện "load more" disabled — v2 wire cursor).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Cap kết quả — cân giữa "user thấy đủ" vs "server không quá tải khi q khớp nhiều".
_MAX_RESULTS_PER_KIND = 20


@dataclass(frozen=True)
class ConversationHit:
    """Match conversation qua title. `matched='title'` cố định — tương lai có thể
    thêm `matched='message'` nếu grouping search-across-messages theo conv."""

    conversation_id: int
    title: str
    updated_at: datetime


@dataclass(frozen=True)
class MessageHit:
    """Match message qua body. `snippet` là `ts_headline` output — chứa `<em>...</em>`
    highlight; UI render như HTML sau khi sanitize (chỉ `<em>` được whitelist)."""

    message_id: int
    conversation_id: int
    snippet: str
    created_at: datetime


@dataclass(frozen=True)
class SearchResult:
    conversations: list[ConversationHit]
    messages: list[MessageHit]


def _escape_like(q: str) -> str:
    """Escape `%`, `_`, `\\` cho LIKE. Không escape `'` (bind param handle)."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class AssistantSearch:
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

    async def search(self, query: str) -> SearchResult:
        """Search conversation titles (ILIKE) + message content (FTS).

        Empty query ⇒ trả SearchResult rỗng (không query DB). Endpoint validate cap len
        trước; repo chỉ chạy nếu q non-empty.
        """
        q = query.strip()
        if not q:
            return SearchResult(conversations=[], messages=[])

        conv_stmt = text(
            """
            SELECT conversation_id, title, updated_at
            FROM assistant.conversations
            WHERE user_id = :user_id
              AND deleted_at IS NULL
              AND title IS NOT NULL
              AND lower(title) LIKE lower(:like_pattern) ESCAPE '\\'
            ORDER BY updated_at DESC
            LIMIT :limit
            """
        ).bindparams(
            bindparam("user_id"),
            bindparam("like_pattern"),
            bindparam("limit"),
        )

        msg_stmt = text(
            """
            SELECT
                m.message_id,
                m.conversation_id,
                ts_headline(
                    'simple',
                    m.content,
                    plainto_tsquery('simple', :q),
                    'StartSel=<em>, StopSel=</em>, MaxWords=25, MinWords=10, '
                    'ShortWord=2, HighlightAll=false, MaxFragments=1'
                ) AS snippet,
                m.created_at
            FROM assistant.messages m
            JOIN assistant.conversations c
              ON c.conversation_id = m.conversation_id
            WHERE m.user_id = :user_id
              AND c.deleted_at IS NULL
              AND to_tsvector('simple', m.content) @@ plainto_tsquery('simple', :q)
            ORDER BY m.created_at DESC
            LIMIT :limit
            """
        ).bindparams(
            bindparam("q"),
            bindparam("user_id"),
            bindparam("limit"),
        )

        async with self._sm() as session:
            conv_rows = (
                await session.execute(
                    conv_stmt,
                    {
                        "user_id": self._user_scope,
                        "like_pattern": f"%{_escape_like(q)}%",
                        "limit": _MAX_RESULTS_PER_KIND,
                    },
                )
            ).all()
            msg_rows = (
                await session.execute(
                    msg_stmt,
                    {
                        "q": q,
                        "user_id": self._user_scope,
                        "limit": _MAX_RESULTS_PER_KIND,
                    },
                )
            ).all()

        return SearchResult(
            conversations=[
                ConversationHit(
                    conversation_id=int(r[0]),
                    title=r[1],
                    updated_at=r[2],
                )
                for r in conv_rows
            ],
            messages=[
                MessageHit(
                    message_id=int(r[0]),
                    conversation_id=int(r[1]),
                    snippet=r[2],
                    created_at=r[3],
                )
                for r in msg_rows
            ],
        )
