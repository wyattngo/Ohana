"""Wiki ingest — text → chunks → embeddings → DB rows.

Platform-shared wiki content lives at the sentinel `shop_id="_platform"` (Phase 2's tenant
hard-filter still applies — the shared corpus is queried by a retriever scoped to that
sentinel, not by relaxing the tenant guard). Per-shop wiki extensions can land later by
passing a real `shop_id`; the read path (tools/wiki.search_wiki) chooses the scope.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import Embedding
from parsing.chunk import chunk_text

PLATFORM_SHOP_ID = "_platform"
PLATFORM_WIKI_NAMESPACE = "platform_wiki"


class _EmbedderProto(Protocol):
    # Chỉ khai method mà hàm này THỰC SỰ gọi. Ingest là bên CORPUS ⇒ `embed_documents`.
    # Khai thêm `embed` sẽ bắt mọi fake phải có cả hai mà không ai dùng tới.
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


async def ingest_wiki(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: _EmbedderProto,
    *,
    text: str,
    source_ref: str,
    shop_id: str = PLATFORM_SHOP_ID,
    max_chars: int = 800,
) -> int:
    """Chunk `text`, embed each chunk, insert Embedding rows. Returns the chunk count.

    Empty/whitespace-only text is a no-op (returns 0). Single-commit per ingest — either
    all chunks land or none do, so a mid-ingest failure never leaves a partial doc.
    """
    chunks = chunk_text(text, max_chars=max_chars)
    if not chunks:
        return 0

    # `embed_documents`, KHÔNG `embed`: đây là bên CORPUS. Với adapter e5 nó gắn
    # `passage: `; với OpenAI nó delegate y nguyên về `embed()`. Gọi thẳng `embed()` ở đây
    # sẽ làm corpus mất dấu vai và lệch không gian so với query (spec 08 §7 E0).
    vectors = await embedder.embed_documents(chunks)
    if len(vectors) != len(chunks):
        raise RuntimeError(f"embedder returned {len(vectors)} vectors for {len(chunks)} chunks")

    async with session_factory() as s:
        for chunk, vec in zip(chunks, vectors, strict=True):
            s.add(
                Embedding(
                    shop_id=shop_id,
                    namespace=PLATFORM_WIKI_NAMESPACE,
                    source_ref=source_ref,
                    chunk=chunk,
                    embedding=vec,
                )
            )
        await s.commit()

    return len(chunks)
