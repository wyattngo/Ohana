"""Tầng 2 Phase 2.4c · assistant_conversations — CRUD repo.

Cover:
- create: trả row với `conversation_id > 0` + `created_at ≈ updated_at`.
- list_recent: sort updated_at DESC, cursor pagination `before_updated_at`.
- U-scope hard filter: user A list KHÔNG thấy user B, get id user B ⇒ None.
- get: cross-user và soft-deleted đều trả None (endpoint 404 cả hai).
- update_title: bump `updated_at`; 404 cross-user.
- soft_delete: set `deleted_at`; xoá lại ⇒ False; sau xoá get ⇒ None; list không thấy.

Postgres integration cùng bài `test_assistant_memory.py` — bảng đã có ở conftest
`_tables_exist_up_front`.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent.assistant_conversations import (
    AssistantConversations,
    _debug_purge_for_user,
)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://ohana:ohana@localhost:5432/ohana_test"
)


@pytest.fixture
async def session_factory() -> async_sessionmaker:
    engine = create_async_engine(DATABASE_URL, echo=False)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def cleanup_conversations(session_factory: async_sessionmaker) -> None:
    """Xoá conversation test trước/sau. Prefix `p24c-` để không chạm dữ liệu khác."""
    from sqlalchemy import text

    async with session_factory() as s:
        await s.execute(text("DELETE FROM assistant.conversations WHERE user_id LIKE 'p24c-%'"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM assistant.conversations WHERE user_id LIKE 'p24c-%'"))
        await s.commit()


@pytest.mark.asyncio
async def test_create_returns_row(
    session_factory: async_sessionmaker, cleanup_conversations: None
) -> None:
    """Create trả ConversationRow với id > 0 + created_at ~ updated_at."""
    repo = AssistantConversations(session_factory, user_scope="p24c-alice")
    row = await repo.create(title="Chat 1")
    assert row.conversation_id > 0
    assert row.title == "Chat 1"
    # server_default now() có thể có drift µs — chấp nhận sai ≤ 1s.
    assert abs((row.updated_at - row.created_at).total_seconds()) < 1.0


@pytest.mark.asyncio
async def test_create_empty_title_becomes_none(
    session_factory: async_sessionmaker, cleanup_conversations: None
) -> None:
    """title="" hoặc None ⇒ lưu NULL (UI hiển thị "Untitled")."""
    repo = AssistantConversations(session_factory, user_scope="p24c-alice")
    row_empty = await repo.create(title="")
    row_none = await repo.create(title=None)
    assert row_empty.title is None
    assert row_none.title is None


@pytest.mark.asyncio
async def test_list_sort_by_updated_desc(
    session_factory: async_sessionmaker, cleanup_conversations: None
) -> None:
    """List sắp xếp updated_at DESC — item tạo sau đứng đầu."""
    repo = AssistantConversations(session_factory, user_scope="p24c-alice")
    a = await repo.create(title="A")
    await asyncio.sleep(0.01)
    b = await repo.create(title="B")
    rows = await repo.list_recent(limit=10)
    ids = [r.conversation_id for r in rows]
    assert ids[0] == b.conversation_id
    assert ids[1] == a.conversation_id


@pytest.mark.asyncio
async def test_list_user_scope_hard_filter(
    session_factory: async_sessionmaker, cleanup_conversations: None
) -> None:
    """User A list KHÔNG thấy conversation của user B (U-scope hard filter)."""
    repo_a = AssistantConversations(session_factory, user_scope="p24c-alice")
    repo_b = AssistantConversations(session_factory, user_scope="p24c-bob")
    await repo_a.create(title="alice-only")
    b_conv = await repo_b.create(title="bob-only")

    a_rows = await repo_a.list_recent(limit=10)
    a_titles = [r.title for r in a_rows]
    assert "bob-only" not in a_titles
    assert "alice-only" in a_titles

    b_rows = await repo_b.list_recent(limit=10)
    assert len(b_rows) == 1
    assert b_rows[0].conversation_id == b_conv.conversation_id


@pytest.mark.asyncio
async def test_list_pagination_by_cursor(
    session_factory: async_sessionmaker, cleanup_conversations: None
) -> None:
    """Cursor pagination: page 1 lấy 2, page 2 dùng updated_at của item cuối ⇒ trả row 3."""
    repo = AssistantConversations(session_factory, user_scope="p24c-alice")
    created = []
    for i in range(3):
        row = await repo.create(title=f"C{i}")
        created.append(row)
        await asyncio.sleep(0.01)

    page1 = await repo.list_recent(limit=2)
    assert len(page1) == 2
    # DESC — C2 (mới nhất) rồi C1.
    assert page1[0].title == "C2"
    assert page1[1].title == "C1"

    cursor = page1[-1].updated_at
    page2 = await repo.list_recent(limit=2, before_updated_at=cursor)
    assert len(page2) == 1
    assert page2[0].title == "C0"


@pytest.mark.asyncio
async def test_list_limit_clamped(
    session_factory: async_sessionmaker, cleanup_conversations: None
) -> None:
    """limit ngoài range ⇒ clamp [1, 100]. limit=0 ⇒ trả tối thiểu 1 item, limit=999 ⇒ OK."""
    repo = AssistantConversations(session_factory, user_scope="p24c-alice")
    await repo.create(title="only")
    # limit=0 clamp lên 1
    rows = await repo.list_recent(limit=0)
    assert len(rows) == 1
    # limit=999 clamp về 100 — chỉ có 1 row, trả 1
    rows = await repo.list_recent(limit=999)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_get_returns_none_cross_user(
    session_factory: async_sessionmaker, cleanup_conversations: None
) -> None:
    """User A get id của user B ⇒ None (endpoint 404 — không leak existence)."""
    repo_a = AssistantConversations(session_factory, user_scope="p24c-alice")
    repo_b = AssistantConversations(session_factory, user_scope="p24c-bob")
    b_conv = await repo_b.create(title="bob")
    assert await repo_a.get(b_conv.conversation_id) is None
    # Sanity: user B lấy được.
    assert await repo_b.get(b_conv.conversation_id) is not None


@pytest.mark.asyncio
async def test_get_returns_none_when_soft_deleted(
    session_factory: async_sessionmaker, cleanup_conversations: None
) -> None:
    """Soft-delete ⇒ get trả None (deleted_at IS NULL hard filter)."""
    repo = AssistantConversations(session_factory, user_scope="p24c-alice")
    conv = await repo.create(title="ephemeral")
    assert await repo.soft_delete(conv.conversation_id) is True
    assert await repo.get(conv.conversation_id) is None


@pytest.mark.asyncio
async def test_get_returns_none_for_missing_id(
    session_factory: async_sessionmaker, cleanup_conversations: None
) -> None:
    """id không tồn tại ⇒ None."""
    repo = AssistantConversations(session_factory, user_scope="p24c-alice")
    assert await repo.get(999_999_999) is None


@pytest.mark.asyncio
async def test_update_title_bumps_updated_at(
    session_factory: async_sessionmaker, cleanup_conversations: None
) -> None:
    """PATCH title bump updated_at (item nhảy lên đầu list)."""
    repo = AssistantConversations(session_factory, user_scope="p24c-alice")
    conv = await repo.create(title="old")
    original_updated = conv.updated_at
    await asyncio.sleep(0.05)
    assert await repo.update_title(conv.conversation_id, "new") is True
    fetched = await repo.get(conv.conversation_id)
    assert fetched is not None
    assert fetched.title == "new"
    assert fetched.updated_at > original_updated


@pytest.mark.asyncio
async def test_update_title_returns_false_cross_user(
    session_factory: async_sessionmaker, cleanup_conversations: None
) -> None:
    """Cross-user PATCH ⇒ False (endpoint 404). Title thực tế không đổi."""
    repo_a = AssistantConversations(session_factory, user_scope="p24c-alice")
    repo_b = AssistantConversations(session_factory, user_scope="p24c-bob")
    b_conv = await repo_b.create(title="bob-title")
    assert await repo_a.update_title(b_conv.conversation_id, "hacked") is False
    # Bob get vẫn thấy title cũ.
    fetched = await repo_b.get(b_conv.conversation_id)
    assert fetched is not None
    assert fetched.title == "bob-title"


@pytest.mark.asyncio
async def test_update_title_empty_raises(
    session_factory: async_sessionmaker, cleanup_conversations: None
) -> None:
    """title rỗng/whitespace ⇒ ValueError (repo-level, endpoint 422 do Pydantic sớm hơn)."""
    repo = AssistantConversations(session_factory, user_scope="p24c-alice")
    conv = await repo.create(title="X")
    with pytest.raises(ValueError, match="title must be non-empty"):
        await repo.update_title(conv.conversation_id, "   ")


@pytest.mark.asyncio
async def test_soft_delete_flips_deleted_at(
    session_factory: async_sessionmaker, cleanup_conversations: None
) -> None:
    """soft_delete ⇒ True lần 1, False lần 2 (không idempotent — client thấy 404 lần 2)."""
    repo = AssistantConversations(session_factory, user_scope="p24c-alice")
    conv = await repo.create(title="X")
    assert await repo.soft_delete(conv.conversation_id) is True
    assert await repo.soft_delete(conv.conversation_id) is False


@pytest.mark.asyncio
async def test_soft_delete_returns_false_cross_user(
    session_factory: async_sessionmaker, cleanup_conversations: None
) -> None:
    """Cross-user DELETE ⇒ False + row Bob còn nguyên."""
    repo_a = AssistantConversations(session_factory, user_scope="p24c-alice")
    repo_b = AssistantConversations(session_factory, user_scope="p24c-bob")
    b_conv = await repo_b.create(title="bob")
    assert await repo_a.soft_delete(b_conv.conversation_id) is False
    assert await repo_b.get(b_conv.conversation_id) is not None


@pytest.mark.asyncio
async def test_empty_user_scope_raises(session_factory: async_sessionmaker) -> None:
    """user_scope="" ⇒ ValueError (không có default; hai lỗi mới leak cross-user)."""
    with pytest.raises(ValueError, match="user_scope is required"):
        AssistantConversations(session_factory, user_scope="")


@pytest.mark.asyncio
async def test_debug_purge_hard_deletes_all(
    session_factory: async_sessionmaker, cleanup_conversations: None
) -> None:
    """`_debug_purge_for_user` xoá HARD cả soft-deleted + sống. Test-only helper."""
    repo = AssistantConversations(session_factory, user_scope="p24c-alice")
    await repo.create(title="live")
    dead = await repo.create(title="dead")
    await repo.soft_delete(dead.conversation_id)
    await _debug_purge_for_user(session_factory, "p24c-alice")
    rows = await repo.list_recent(limit=100)
    assert rows == []
