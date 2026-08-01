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

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.embedder import Embedder
from db.models import AssistantUserMemory


@dataclass(frozen=True)
class MemoryHit:
    """Một memory match. `score` là cosine distance — nhỏ hơn = gần hơn."""

    memory_id: int
    content: str
    score: float
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
