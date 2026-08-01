"""Tầng 2 Phase 2.3 · assistant_memory — save + recall gate.

Cover:
- save: trả memory_id, dùng `embed_documents` (I11 · passage prefix).
- recall: k-nearest cosine, ordered nearest first, dùng `embed_query` (I11 · query prefix).
- U-scope hard filter: user A save, user B recall → 0 hits kể cả gần hơn về vector.
- k=0 → [] (không round-trip DB); k>0 nhưng chưa save → [].
- Constructor validation: empty user_scope raise; empty content/query raise.

Postgres integration (pgvector) + `FakeEmbedder` deterministic (từ test_wiki_rag.py) —
không cần network, tái lập được, và cho phép chọn semantic overlap có kiểm soát để
assert thứ hạng recall.

DATABASE_URL trỏ DB test kết thúc `_test` (conftest.py `_tables_exist_up_front` cưỡng
chế) — same env với test_wiki_rag/test_tenant_isolation.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent.assistant_memory import AssistantMemory, MemoryHit
from agent.embedder import Embedder
from app.config import EMBED_DIM

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://ohana:ohana@localhost:5432/ohana_test"
)


class FakeEmbedder(Embedder):
    """Deterministic sparse embedder: token hash → slot = 1.0. Overlap token ⇒ vector
    gần hơn. Mượn hình dạng từ `tests/test_wiki_rag.py::FakeEmbedder` để cùng bài đo."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            vec = [0.0] * EMBED_DIM
            for tok in t.lower().split():
                slot = hash(tok) % EMBED_DIM
                vec[slot] = 1.0
            out.append(vec)
        return out


@pytest.fixture
async def session_factory() -> async_sessionmaker:
    """Engine + factory cho một test. Bảng `assistant.user_memory` đã có từ
    conftest.py session-scope `_tables_exist_up_front` (metadata.create_all)."""
    engine = create_async_engine(DATABASE_URL, echo=False)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def cleanup_memory(session_factory: async_sessionmaker) -> None:
    """Xoá memory test trước/sau. Phạm vi hẹp theo prefix user_id để không chạm dữ liệu
    khác trên DB dùng chung."""
    from sqlalchemy import text

    async with session_factory() as s:
        await s.execute(text("DELETE FROM assistant.user_memory WHERE user_id LIKE 'p23test-%'"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM assistant.user_memory WHERE user_id LIKE 'p23test-%'"))
        await s.commit()


@pytest.mark.asyncio
async def test_save_returns_memory_id(
    session_factory: async_sessionmaker, cleanup_memory: None
) -> None:
    """Save trả memory_id (bigint IDENTITY) > 0."""
    memory = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-alice")
    mid = await memory.save_text("Alice thích trà sữa trân châu đen")
    assert isinstance(mid, int)
    assert mid > 0


@pytest.mark.asyncio
async def test_recall_returns_hits_ordered_by_similarity(
    session_factory: async_sessionmaker, cleanup_memory: None
) -> None:
    """Save 3 memory với semantics phân biệt, query gần với 1 câu ⇒ câu đó ở đầu."""
    memory = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-alice")
    await memory.save_text("Alice thích chạy bộ buổi sáng ở công viên")
    await memory.save_text("Alice ăn chay từ năm 2020")
    await memory.save_text("Alice có mèo tên Miu màu cam")

    hits = await memory.recall_text("Alice nuôi mèo tên gì màu cam", k=3)
    assert len(hits) == 3
    # Câu về mèo có nhiều token overlap nhất (Alice/mèo/màu/cam) ⇒ score nhỏ nhất.
    assert "Miu" in hits[0].content
    # Score tăng dần (cosine distance nhỏ hơn = gần hơn).
    assert hits[0].score <= hits[1].score <= hits[2].score


@pytest.mark.asyncio
async def test_user_scope_hard_filter(
    session_factory: async_sessionmaker, cleanup_memory: None
) -> None:
    """User A save; user B recall — dù query khớp hoàn hảo, 0 hit vì WHERE user_id."""
    alice = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-alice")
    await alice.save_text("secret memory của Alice không được leak")

    bob = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-bob")
    hits = await bob.recall_text("secret memory của Alice không được leak", k=5)
    assert hits == []


@pytest.mark.asyncio
async def test_recall_respects_k_limit(
    session_factory: async_sessionmaker, cleanup_memory: None
) -> None:
    """Save 5, recall k=2 ⇒ chính xác 2 hits."""
    memory = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-alice")
    for i in range(5):
        await memory.save_text(f"memory số {i} về chủ đề bất kỳ")
    hits = await memory.recall_text("chủ đề", k=2)
    assert len(hits) == 2


@pytest.mark.asyncio
async def test_recall_empty_when_no_memories(
    session_factory: async_sessionmaker, cleanup_memory: None
) -> None:
    """User chưa save memory nào ⇒ recall trả [] (không raise)."""
    memory = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-fresh")
    hits = await memory.recall_text("bất kỳ query nào", k=10)
    assert hits == []


@pytest.mark.asyncio
async def test_recall_zero_k_returns_empty_without_db_call(
    session_factory: async_sessionmaker,
) -> None:
    """k=0 short-circuit tại Python ⇒ không round-trip DB (không cần fixture cleanup vì
    không insert). Embedder cũng KHÔNG được gọi — không tốn cost."""
    embedder_mock = AsyncMock(spec=Embedder)
    memory = AssistantMemory(session_factory, embedder_mock, user_scope="p23test-x")
    hits = await memory.recall_text("query", k=0)
    assert hits == []
    embedder_mock.embed_query.assert_not_called()
    embedder_mock.embed_documents.assert_not_called()


def test_constructor_requires_user_scope_non_empty(
    session_factory: async_sessionmaker,
) -> None:
    """`user_scope=""` ⇒ ValueError (không default silent scope-rỗng khớp mọi row)."""
    with pytest.raises(ValueError, match="user_scope is required"):
        AssistantMemory(session_factory, FakeEmbedder(), user_scope="")


@pytest.mark.asyncio
async def test_save_rejects_empty_content(
    session_factory: async_sessionmaker,
) -> None:
    """`content=""` hay chỉ whitespace ⇒ ValueError (memory rỗng làm recall bẩn)."""
    memory = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-x")
    with pytest.raises(ValueError, match="content must be non-empty"):
        await memory.save_text("")
    with pytest.raises(ValueError, match="content must be non-empty"):
        await memory.save_text("   ")


@pytest.mark.asyncio
async def test_recall_rejects_empty_query(
    session_factory: async_sessionmaker,
) -> None:
    """`query=""` hay whitespace ⇒ ValueError (query rỗng recall vô định)."""
    memory = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-x")
    with pytest.raises(ValueError, match="query must be non-empty"):
        await memory.recall_text("", k=5)
    with pytest.raises(ValueError, match="query must be non-empty"):
        await memory.recall_text("   ", k=5)


@pytest.mark.asyncio
async def test_save_uses_embed_documents_prefix(
    session_factory: async_sessionmaker,
) -> None:
    """I11 · save phải dùng `embed_documents` (passage), KHÔNG `embed_query`/`embed`.
    Refactor sau lỡ đổi ⇒ recall tệ ÂM THẦM; test này bắt được."""
    embedder_mock = AsyncMock(spec=Embedder)
    embedder_mock.embed_documents.return_value = [[0.1] * EMBED_DIM]

    memory = AssistantMemory(session_factory, embedder_mock, user_scope="p23test-i11-save")
    try:
        await memory.save_text("test content")
    finally:
        # Cleanup dòng vừa insert.
        from sqlalchemy import text

        async with session_factory() as s:
            await s.execute(
                text("DELETE FROM assistant.user_memory WHERE user_id = 'p23test-i11-save'")
            )
            await s.commit()

    embedder_mock.embed_documents.assert_awaited_once_with(["test content"])
    embedder_mock.embed_query.assert_not_called()
    embedder_mock.embed.assert_not_called()


@pytest.mark.asyncio
async def test_recall_uses_embed_query_prefix(
    session_factory: async_sessionmaker,
) -> None:
    """I11 · recall phải dùng `embed_query` (query), KHÔNG `embed_documents`/`embed`."""
    embedder_mock = AsyncMock(spec=Embedder)
    embedder_mock.embed_query.return_value = [0.1] * EMBED_DIM

    memory = AssistantMemory(session_factory, embedder_mock, user_scope="p23test-i11-recall")
    hits = await memory.recall_text("test query", k=5)
    assert hits == []  # chưa save gì

    embedder_mock.embed_query.assert_awaited_once_with("test query")
    embedder_mock.embed_documents.assert_not_called()
    embedder_mock.embed.assert_not_called()


@pytest.mark.asyncio
async def test_memory_hit_dataclass_shape(
    session_factory: async_sessionmaker, cleanup_memory: None
) -> None:
    """`MemoryHit` frozen dataclass với 4 field đúng type — hợp đồng cho caller (P2.4)."""
    memory = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-alice")
    await memory.save_text("một câu để verify shape")
    hits = await memory.recall_text("câu để verify", k=1)
    assert len(hits) == 1
    hit = hits[0]
    assert isinstance(hit, MemoryHit)
    assert isinstance(hit.memory_id, int) and hit.memory_id > 0
    assert isinstance(hit.content, str) and hit.content
    assert isinstance(hit.score, float)
    # `created_at` tzaware (server_default now() với timestamptz).
    assert hit.created_at.tzinfo is not None


# =====================================================================================
# Phase 2.4c · list_memories + delete_memory extension.
# =====================================================================================


@pytest.mark.asyncio
async def test_list_memories_sorted_desc(
    session_factory: async_sessionmaker, cleanup_memory: None
) -> None:
    """list_memories sắp xếp created_at DESC — memory mới nhất đứng đầu."""
    import asyncio

    memory = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-list")
    await memory.save_text("first")
    await asyncio.sleep(0.01)
    await memory.save_text("second")
    await asyncio.sleep(0.01)
    await memory.save_text("third")
    rows = await memory.list_memories(limit=10)
    contents = [r.content for r in rows]
    assert contents == ["third", "second", "first"]


@pytest.mark.asyncio
async def test_list_memories_user_scope_hard_filter(
    session_factory: async_sessionmaker, cleanup_memory: None
) -> None:
    """User A list KHÔNG thấy memory user B (cùng discipline recall_text)."""
    mem_a = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-scope-a")
    mem_b = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-scope-b")
    await mem_a.save_text("alice-only")
    await mem_b.save_text("bob-only")
    a_rows = await mem_a.list_memories(limit=10)
    assert [r.content for r in a_rows] == ["alice-only"]


@pytest.mark.asyncio
async def test_list_memories_cursor_pagination(
    session_factory: async_sessionmaker, cleanup_memory: None
) -> None:
    """Cursor pagination: page 1 (limit=2), page 2 dùng cursor = created_at last."""
    import asyncio

    memory = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-page")
    for i in range(3):
        await memory.save_text(f"m{i}")
        await asyncio.sleep(0.01)
    page1 = await memory.list_memories(limit=2)
    assert len(page1) == 2
    cursor = page1[-1].created_at
    page2 = await memory.list_memories(limit=2, before_created_at=cursor)
    assert len(page2) == 1


@pytest.mark.asyncio
async def test_delete_memory_hard_removes_row(
    session_factory: async_sessionmaker, cleanup_memory: None
) -> None:
    """delete_memory HARD DELETE — sau delete list không thấy + recall không match."""
    memory = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-del")
    mid = await memory.save_text("forget me")
    assert await memory.delete_memory(mid) is True
    rows = await memory.list_memories(limit=10)
    assert all(r.memory_id != mid for r in rows)
    # Recall cũng không match (hard delete = biến mất khỏi HNSW).
    hits = await memory.recall_text("forget me", k=5)
    assert all(h.memory_id != mid for h in hits)


@pytest.mark.asyncio
async def test_delete_memory_returns_false_cross_user(
    session_factory: async_sessionmaker, cleanup_memory: None
) -> None:
    """User A delete memory_id của user B ⇒ False + row Bob còn nguyên."""
    mem_a = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-cross-a")
    mem_b = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-cross-b")
    b_mid = await mem_b.save_text("bob-secret")
    assert await mem_a.delete_memory(b_mid) is False
    # Bob list vẫn thấy.
    b_rows = await mem_b.list_memories(limit=10)
    assert any(r.memory_id == b_mid for r in b_rows)


@pytest.mark.asyncio
async def test_delete_memory_returns_false_for_missing_id(
    session_factory: async_sessionmaker, cleanup_memory: None
) -> None:
    """memory_id không tồn tại ⇒ False."""
    memory = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-miss")
    assert await memory.delete_memory(999_999_999) is False


@pytest.mark.asyncio
async def test_list_memories_limit_clamped(
    session_factory: async_sessionmaker, cleanup_memory: None
) -> None:
    """limit ngoài range ⇒ clamp [1, 100] cùng bài conversations."""
    memory = AssistantMemory(session_factory, FakeEmbedder(), user_scope="p23test-clamp")
    await memory.save_text("only")
    rows = await memory.list_memories(limit=0)
    assert len(rows) == 1
    rows = await memory.list_memories(limit=999)
    assert len(rows) == 1
