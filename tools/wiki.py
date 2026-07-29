"""F1 Wiki RAG read-tool — `search_wiki(query)` over the platform's shared wiki corpus.

The wiki lives at the sentinel `shop_id="_platform"` (see parsing.ingest). The retriever
is constructed with `shop_scope="_platform"` so Phase 2's SQL WHERE tenant guard still
applies — chat rows from any real shop cannot bleed into wiki results even if their vector
is nearer. Namespace is pinned to `platform_wiki`; per-shop wiki extensions (if we add them
later) will need a distinct scope + a separate helper.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from parsing.ingest import PLATFORM_SHOP_ID, PLATFORM_WIKI_NAMESPACE
from retrieval.base import Hit
from retrieval.pgvector import PgvectorRetriever
from tools.registry import Tool

_DEFAULT_K = 5


class _EmbedderProto(Protocol):
    # Chỉ khai method mà hàm này THỰC SỰ gọi. Search là bên QUERY ⇒ `embed_query`.
    async def embed_query(self, text: str) -> list[float]: ...


async def search_wiki(
    query: str,
    *,
    embedder: _EmbedderProto,
    session_factory: async_sessionmaker[AsyncSession],
    k: int = _DEFAULT_K,
) -> list[Hit]:
    """Embed `query`, retrieve top-k chunks from the shared platform wiki."""
    if not query or not query.strip():
        return []
    # `embed_query`, KHÔNG `embed`: đây là bên QUERY. Phải khớp vai với
    # `parsing/ingest.py` (`embed_documents`) — lệch vai không crash, chỉ làm retrieval tệ
    # đi âm thầm (spec 08 §7 E0, gate bất đối xứng).
    vec = await embedder.embed_query(query)
    retriever = PgvectorRetriever(session_factory, shop_scope=PLATFORM_SHOP_ID)
    return await retriever.search(namespaces=[PLATFORM_WIKI_NAMESPACE], query=vec, k=k)


_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Natural-language question or keyword for the platform wiki.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


def build_tool(
    embedder: _EmbedderProto,
    session_factory: async_sessionmaker[AsyncSession],
) -> Tool:
    """Wrap `search_wiki` as a Tool for the registry. The (user_id, shop_id) args from the
    orchestrator are IGNORED for platform-wide wiki search — the corpus is shared. Per-shop
    wiki extensions would need a distinct tool that DOES use shop_id."""

    async def handler(user_id: str, shop_id: str, args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query")
        if not isinstance(query, str):
            return {"success": False, "error": "invalid_query"}
        hits = await search_wiki(query, embedder=embedder, session_factory=session_factory)
        return {
            "success": True,
            "data": [
                {"source_ref": h.source_ref, "chunk": h.chunk, "score": h.score} for h in hits
            ],
        }

    return Tool(
        name="search_wiki",
        description="Search the platform wiki for policy, FAQ, and product info.",
        parameters=_PARAMETERS,
        handler=handler,
        kind="read",
    )
