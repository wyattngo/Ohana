"""Memory per-user Tầng 2 — save + recall (ADR §3, Phase 2.3).

Primitive semantic memory cho trợ lý AI (chưa consume; P2.4 chat router dùng).
Sit cùng tier với `assistant_cost` và `assistant_rate_limit` (importlinter I1c cover
luồng A).

**E5 asymmetric embed (I11).** SAVE dùng `embed_documents` (passage prefix), RECALL
dùng `embed_query` (query prefix). Trộn hai bên KHÔNG crash và KHÔNG sai type — nó
chỉ làm recall tệ đi âm thầm (spec 08 §5.4). Test tường minh (assert method gọi đúng)
để refactor sau lỡ đổi thành `embed()` phẳng bắt được.

**U-scope hard filter** (analog `PgvectorRetriever.shop_scope`). `user_scope` bắt
buộc ở constructor — hai lỗi (không type-permitted) mới leak được cross-user memory:
(1) khai `user_scope=""` (ValueError), (2) SQL WHERE quên `user_id =` (constant WHERE
trong `recall_text` cưỡng chế). WHERE đứng TRƯỚC `ORDER BY dist LIMIT k` để một memory
user khác gần hơn về vector KHÔNG outrank memory in-scope.

**Append-only tại P2.3.** Không có DELETE / `forgotten_at` — ADR model comment nói P2.4
quyết. Save trả `memory_id` để P2.4 forget-endpoint tham chiếu chính xác.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.embedder import Embedder
from db.models import AssistantUserMemory

_LIST_LIMIT_MAX = 100


@dataclass(frozen=True)
class MemoryHit:
    """Một memory match. `score` là cosine distance — nhỏ hơn = gần hơn."""

    memory_id: int
    content: str
    score: float
    created_at: datetime


@dataclass(frozen=True)
class MemoryRow:
    """Một memory record (KHÔNG kèm `embedding` — vector 1024f nặng và không hữu ích cho
    UI list; recall dùng path riêng qua HNSW). Tách kiểu ≠ `MemoryHit` (không có `score`)
    để endpoint list không lỡ trả field distance vô nghĩa (không có query)."""

    memory_id: int
    content: str
    created_at: datetime


class AssistantMemory:
    """Save/recall memory semantic per-user, HNSW cosine trên `assistant.user_memory`.

    Constructor pattern giống `retrieval/pgvector.py::PgvectorRetriever`: scope BẮT
    BUỘC ở __init__, không phải per-call arg. Không thể dựng instance mà không chọn
    user; một instance chỉ recall/save cho user đó.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        *,
        user_scope: str,
    ) -> None:
        if not user_scope:
            raise ValueError("user_scope is required — no default, no cross-user surface")
        self._sm = session_factory
        self._embedder = embedder
        self._user_scope = user_scope

    async def save_text(self, content: str) -> int:
        """Embed `content` (I11 · passage prefix) + INSERT. Trả `memory_id` sinh mới.

        Empty/whitespace content ⇒ `ValueError`. Ghi memory rỗng không có nghĩa và làm
        recall bẩn (vector rỗng distance vô định); từ chối ở entrypoint là cách rẻ nhất.
        """
        if not content or not content.strip():
            raise ValueError("content must be non-empty")
        (vec,) = await self._embedder.embed_documents([content])
        stmt = (
            insert(AssistantUserMemory)
            .values(user_id=self._user_scope, content=content, embedding=vec)
            .returning(AssistantUserMemory.memory_id)
        )
        async with self._sm() as session:
            memory_id = (await session.execute(stmt)).scalar_one()
            await session.commit()
        return int(memory_id)

    async def recall_text(self, query: str, k: int) -> list[MemoryHit]:
        """`k` memory gần nhất với `query` (I11 · query prefix) TRONG scope user hiện tại.

        Empty query ⇒ `ValueError` (cùng lý do save). `k <= 0` ⇒ trả `[]` tại chỗ,
        không round-trip DB (LIMIT 0 hợp lệ về SQL nhưng gọi vô nghĩa).

        `WHERE user_id = user_scope` ĐỨNG TRƯỚC `ORDER BY dist LIMIT k` — Postgres áp
        WHERE trước khi ORDER, nên một memory user khác gần hơn về vector KHÔNG bao giờ
        outrank memory in-scope. Đây là gate của contract; test `test_user_scope_hard_
        filter` bắt regress.
        """
        if not query or not query.strip():
            raise ValueError("query must be non-empty")
        if k <= 0:
            return []
        query_vec = await self._embedder.embed_query(query)
        dist = AssistantUserMemory.embedding.cosine_distance(query_vec).label("dist")
        stmt = (
            select(
                AssistantUserMemory.memory_id,
                AssistantUserMemory.content,
                AssistantUserMemory.created_at,
                dist,
            )
            .where(AssistantUserMemory.user_id == self._user_scope)
            .order_by(dist)
            .limit(k)
        )
        async with self._sm() as session:
            rows = (await session.execute(stmt)).all()
        return [
            MemoryHit(
                memory_id=int(r[0]),
                content=r[1],
                created_at=r[2],
                score=float(r[3]),
            )
            for r in rows
        ]

    async def list_memories(
        self,
        limit: int,
        before_created_at: datetime | None = None,
    ) -> list[MemoryRow]:
        """List memory per-user (không vector), sắp xếp `created_at DESC`. Cursor pagination
        bằng `before_created_at` — cùng bài `AssistantConversations.list_recent` (append-only
        memory nên page-drift ít, nhưng dùng cursor giữ contract nhất quán).

        Bind index `idx_assistant_user_memory_user (user_id, created_at DESC)` — tránh full
        table scan khi user có nhiều memory.
        """
        clamped = max(1, min(limit, _LIST_LIMIT_MAX))
        stmt = select(
            AssistantUserMemory.memory_id,
            AssistantUserMemory.content,
            AssistantUserMemory.created_at,
        ).where(AssistantUserMemory.user_id == self._user_scope)
        if before_created_at is not None:
            stmt = stmt.where(AssistantUserMemory.created_at < before_created_at)
        stmt = stmt.order_by(AssistantUserMemory.created_at.desc()).limit(clamped)
        async with self._sm() as session:
            rows = (await session.execute(stmt)).all()
        return [MemoryRow(memory_id=int(r[0]), content=r[1], created_at=r[2]) for r in rows]

    async def delete_memory(self, memory_id: int) -> bool:
        """Hard DELETE (P2.4c ADR decision — không `forgotten_at`). Trả False khi 404 (id
        không tồn tại HOẶC thuộc user khác). WHERE `user_id = user_scope` cưỡng chế —
        cross-user rowcount=0 ⇒ False, endpoint 404 (không leak sự tồn tại)."""
        stmt = delete(AssistantUserMemory).where(
            AssistantUserMemory.memory_id == memory_id,
            AssistantUserMemory.user_id == self._user_scope,
        )
        async with self._sm() as session:
            result = cast("CursorResult[Any]", await session.execute(stmt))
            await session.commit()
        return (result.rowcount or 0) > 0
