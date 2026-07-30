"""Wiki RAG happy-path gate (spec 01 Phase 3 step 9).

Written BEFORE parsing/chunk.py, parsing/ingest.py, tools/registry.py, tools/wiki.py,
api/admin.py — expected RED until steps 10/11 land. The failure mode we're pinning down:
`search_wiki(query)` doesn't surface the chunk that contains the answer.

Contract this test locks in:
  1. `parsing.chunk.chunk_text(text, *, max_chars)` → list[str], non-empty for non-empty input.
  2. `parsing.ingest.ingest_wiki(session_factory, embedder, text, source_ref, shop_id="_platform")`
     → int (number of chunks stored). Persists chunks with namespace="platform_wiki" and the
     supplied `shop_id` (default is the platform sentinel).
  3. `tools.wiki.search_wiki(query, embedder, session_factory)` → list[Hit] against namespace
     "platform_wiki" scoped to `_platform` (SQL WHERE, not post-filter — reuses Phase 2 guard).
  4. Deterministic FakeEmbedder that maps text → vector by keyword overlap, so we can assert the
     wiki chunk containing the answer wins nearest-neighbor without a live OpenAI call.

Requires a live Postgres with pgvector at DATABASE_URL (same env as tenant-isolation gate).
"""

from __future__ import annotations

import os

import pytest

from agent.embedder import Embedder
from app.config import EMBED_DIM

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://ohana:ohana@localhost:5432/ohana"
)

# Đọc từ nguồn sự thật, KHÔNG khai lại số. Trước spec 08 E1 đây là `1536` viết cứng —
# và khi cột DB đổi sang 1024 thì test này vỡ với lỗi dim mismatch, tức nó đang khoá
# một hằng số thay vì khoá một hành vi. Import thì nó đi theo schema.
VECTOR_DIM = EMBED_DIM


class FakeEmbedder(Embedder):
    """Deterministic sparse-ish embedder. Splits text on whitespace, hashes each token into a
    fixed slot in a `VECTOR_DIM`-length vector (value = 1.0). Two texts that share a token also
    share a slot → cosine distance is smaller. No network, no model, fully reproducible.

    Kế thừa `Embedder` (spec 08 E0) chứ KHÔNG duck-type nữa. Bản duck-type chỉ có `embed()`
    đã vỡ ngay khi ABC mọc thêm `embed_query`/`embed_documents` — fake không kế thừa thì mỗi
    lần contract đổi lại phải sửa tay từng chỗ, và chỗ nào quên thì hỏng lúc chạy chứ không
    phải lúc type-check. Kế thừa xong, default delegate của ABC lo phần còn lại.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            vec = [0.0] * VECTOR_DIM
            for tok in t.lower().split():
                slot = hash(tok) % VECTOR_DIM
                vec[slot] = 1.0
            out.append(vec)
        return out


@pytest.mark.asyncio
async def test_wiki_ingest_then_search_returns_hit() -> None:
    """Ingest one wiki doc → search for a keyword in it → the ingested chunk is the top hit."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from db.models import Base
    from parsing.ingest import ingest_wiki
    from tools.wiki import search_wiki

    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    embedder = FakeEmbedder()

    wiki_text = (
        "Ohana is a seller AI copilot for Vietnamese social commerce. "
        "It integrates Zalo OA to draft replies for sellers.\n\n"
        "Return policy: buyers may return items within 7 days of delivery "
        "provided the packaging is intact."
    )
    n = await ingest_wiki(
        session_factory,
        embedder,
        text=wiki_text,
        source_ref="wiki:overview",
    )
    assert n >= 1, "ingest_wiki should return the number of chunks stored"

    hits = await search_wiki(
        "What is the return policy for buyers?",
        embedder=embedder,
        session_factory=session_factory,
    )

    await engine.dispose()

    assert len(hits) >= 1, "search_wiki should surface at least one chunk"
    assert any("return" in h.chunk.lower() for h in hits), (
        "top wiki hits should include the chunk about the return policy"
    )
    for h in hits:
        assert h.namespace == "platform_wiki", (
            f"search_wiki must stay in platform_wiki namespace, got {h.namespace!r}"
        )


@pytest.mark.asyncio
async def test_wiki_search_isolated_from_other_shop_conversations() -> None:
    """search_wiki hits ONLY platform_wiki rows — a conversation chunk in namespace='chat' with
    an even nearer vector must not surface. This locks the phase-2 shop_scope + namespace filter
    into the wiki tool too."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from db.models import Base, Embedding
    from parsing.ingest import ingest_wiki
    from tools.wiki import search_wiki

    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    embedder = FakeEmbedder()

    await ingest_wiki(
        session_factory,
        embedder,
        text="Ohana return policy: buyers may return within 7 days.",
        source_ref="wiki:policy",
    )

    # Adversarial: a chat message for shop_a that shares MORE tokens with the query than the
    # wiki chunk. Without namespace-hard-filter, this would rank first.
    async with session_factory() as s:
        (chat_vec,) = await embedder.embed(
            ["shop_a chat return return return return return policy policy"]
        )
        s.add(
            Embedding(
                shop_id="shop_a",
                namespace="chat",
                source_ref="msg:1",
                chunk="chat noise about return return policy",
                embedding=chat_vec,
            )
        )
        await s.commit()

    hits = await search_wiki("return policy", embedder=embedder, session_factory=session_factory)

    await engine.dispose()

    assert len(hits) >= 1
    assert all(h.namespace == "platform_wiki" for h in hits), (
        "chat namespace bled into search_wiki output — namespace filter is post-filter, not SQL"
    )
