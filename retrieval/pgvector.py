"""pgvector-backed Retriever. The ONLY place that raw-queries the `embeddings` table.

Tenant scope is a CONSTRUCTOR-required argument (`shop_scope`), not a per-call kwarg —
you cannot build a retriever without picking a shop, and one instance can only ever surface
rows for that shop. Combined with the required-`namespaces` argument, it takes two mistakes
(neither of which the type system permits) to leak cross-tenant data. The scope filter
goes into the SQL WHERE clause BEFORE ORDER/LIMIT so a nearer-but-out-of-scope vector
cannot outrank a farther in-scope one — that specific failure mode is what
test_tenant_isolation.test_pgvector_retriever_shop_scope_hard_filter guards against
(R1.22 analog).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import Embedding
from retrieval.base import Hit, Retriever


class PgvectorRetriever(Retriever):
    def __init__(self, sm: async_sessionmaker[AsyncSession], *, shop_scope: str) -> None:
        if not shop_scope:
            raise ValueError("shop_scope is required — no default, no cross-tenant surface")
        self._sm = sm
        self._shop_scope = shop_scope

    async def search(self, namespaces: list[str], query: list[float], k: int) -> list[Hit]:
        if not namespaces:  # no scope → no results (never "all")
            return []
        # Two SQL-level WHEREs applied BEFORE order/limit:
        #   - shop_id == shop_scope  (tenant hard filter — R1.22 analog)
        #   - namespace IN (…)       (kind filter — set by caller)
        # A globally-nearer row from another shop cannot outrank a farther in-scope row.
        dist = Embedding.embedding.cosine_distance(query).label("dist")
        stmt = (
            select(Embedding.namespace, Embedding.source_ref, Embedding.chunk, dist)
            .where(Embedding.shop_id == self._shop_scope)
            .where(Embedding.namespace.in_(namespaces))
            # Drained / not-yet-indexed rows (NULL embedding) have no usable vector — exclude
            # so `cosine_distance` never yields NULL. Guards any partial-reindex window.
            .where(Embedding.embedding.is_not(None))
            .order_by(dist)
            .limit(k)
        )
        async with self._sm() as session:
            rows = (await session.execute(stmt)).all()
        return [Hit(namespace=r[0], source_ref=r[1], chunk=r[2], score=float(r[3])) for r in rows]
