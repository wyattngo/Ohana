"""Spec 17 P0 — ZaloOATokenRepo tests (RISK: medium, RED first).

Vì sao module này tồn tại — không cùng biên với các repo shop-scope.
`ZaloOAToken` giữ credentials nền-tảng (access/refresh/oa_secret) cho MỘT OA. Row lookup
theo `shop_id` PK (từ auth) hoặc theo `oa_id` (từ webhook body chưa verify — chỉ tra key để
verify signature, KHÔNG dùng làm scope). Không phải bảng tenant nên KHÔNG `shop_scope`
constructor; giống `WebhookEventRepo` cùng file.

`update_tokens_locked` PHẢI dùng `SELECT ... FOR UPDATE` — refresh_token của Zalo là
SINGLE-USE, mỗi lần refresh chết cặp cũ. Hai process cùng refresh 1 shop mà không lock =
1 process ghi cặp mới, 1 process ghi cặp KHÁC lên đè, cả hai đều nghĩ mình đúng, nhưng chỉ
có cặp cuối cùng sống — cặp trước bị mất luôn khả năng refresh (Zalo đã invalidate). Test
concurrent phải chứng minh serialize, không phải chỉ "cả hai chạy xong".
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from db.models import Shop, ZaloOAToken
from db.repos import ZaloOATokenRepo


@pytest.mark.asyncio
async def test_get_by_shop_returns_none_for_unknown_shop(fresh_db):
    _, session_factory = await fresh_db()
    async with session_factory() as session:
        repo = ZaloOATokenRepo(session)
        result = await repo.get_by_shop("shop-that-does-not-exist")
        assert result is None


@pytest.mark.asyncio
async def test_upsert_then_get_roundtrips_values(fresh_db):
    _, session_factory = await fresh_db()
    now = datetime.now(UTC)
    access_exp = now + timedelta(hours=1)
    refresh_exp = now + timedelta(days=90)

    async with session_factory() as session:
        # Shop phải tồn tại vì FK CASCADE
        session.add(Shop(id="shop-1", name="Shop 1"))
        await session.commit()

        repo = ZaloOATokenRepo(session)
        await repo.update_tokens_locked(
            shop_id="shop-1",
            oa_id="oa-1234",
            access_token="access-abc",
            refresh_token="refresh-xyz",
            access_expires_at=access_exp,
            refresh_expires_at=refresh_exp,
            oa_secret_key="secret-key-per-oa",
        )

        row = await repo.get_by_shop("shop-1")
        assert row is not None
        assert row.shop_id == "shop-1"
        assert row.oa_id == "oa-1234"
        assert row.access_token == "access-abc"
        assert row.refresh_token == "refresh-xyz"
        assert row.oa_secret_key == "secret-key-per-oa"


@pytest.mark.asyncio
async def test_get_oa_secret_by_oa_id_matches_when_row_exists(fresh_db):
    """P1 signature verify sẽ lookup theo `oa_id` (suy từ sender.id/recipient.id
    trong webhook body). Method này là seam đó — P0 land để P1 dùng ngay."""
    _, session_factory = await fresh_db()
    now = datetime.now(UTC)

    async with session_factory() as session:
        session.add(Shop(id="shop-a", name="Shop A"))
        await session.commit()

        repo = ZaloOATokenRepo(session)
        await repo.update_tokens_locked(
            shop_id="shop-a",
            oa_id="oa-9999",
            access_token="a",
            refresh_token="r",
            access_expires_at=now + timedelta(hours=1),
            refresh_expires_at=now + timedelta(days=90),
            oa_secret_key="the-oa-secret-key",
        )

        secret = await repo.get_oa_secret_by_oa_id("oa-9999")
        assert secret == "the-oa-secret-key"


@pytest.mark.asyncio
async def test_get_oa_secret_by_oa_id_returns_none_when_unknown(fresh_db):
    """`oa_id` không có trong DB ⇒ None — P1 verify sẽ dùng None như tín hiệu 401."""
    _, session_factory = await fresh_db()
    async with session_factory() as session:
        repo = ZaloOATokenRepo(session)
        result = await repo.get_oa_secret_by_oa_id("oa-not-there")
        assert result is None


@pytest.mark.asyncio
async def test_update_tokens_locked_serializes_concurrent_writes(fresh_db):
    """Cốt lõi P0 — SELECT FOR UPDATE phải serialize hai session ghi cùng shop_id.

    Nếu không lock: cả hai đọc trạng thái cũ, tính new_tokens độc lập, ghi đè lẫn nhau
    kiểu last-writer-wins. Chấp nhận được cho counter, KHÔNG chấp nhận được cho refresh
    Zalo single-use (nó CHẾT refresh_token bên thua).

    Test: 2 session cùng update, session B chờ session A commit rồi mới đọc/ghi. Đo bằng
    thứ tự: kết quả cuối cùng phải là values của session ghi SAU (last), không phải merge.
    """
    engine, session_factory = await fresh_db()
    now = datetime.now(UTC)
    access_exp = now + timedelta(hours=1)
    refresh_exp = now + timedelta(days=90)

    async with session_factory() as session:
        session.add(Shop(id="shop-race", name="Race Shop"))
        session.add(
            ZaloOAToken(
                shop_id="shop-race",
                oa_id="oa-race",
                access_token="initial",
                refresh_token="initial",
                access_expires_at=access_exp,
                refresh_expires_at=refresh_exp,
                oa_secret_key="s",
            )
        )
        await session.commit()

    # Barrier để chắc chắn B bắt đầu FOR UPDATE trong lúc A đang giữ lock
    a_locked = asyncio.Event()
    a_may_commit = asyncio.Event()

    async def writer_a():
        async with session_factory() as s:
            repo = ZaloOATokenRepo(s)
            # Bắt đầu transaction + lock row
            await repo._lock_row_for_test("shop-race")  # test-only helper
            a_locked.set()
            await a_may_commit.wait()
            await repo.update_tokens_locked(
                shop_id="shop-race",
                oa_id="oa-race",
                access_token="A-wrote",
                refresh_token="A-wrote",
                access_expires_at=access_exp,
                refresh_expires_at=refresh_exp,
                oa_secret_key="s",
                _reuse_transaction=True,
            )
            await s.commit()

    async def writer_b():
        await a_locked.wait()
        async with session_factory() as s:
            repo = ZaloOATokenRepo(s)
            # Sẽ BLOCK ở đây tới khi A commit
            await repo.update_tokens_locked(
                shop_id="shop-race",
                oa_id="oa-race",
                access_token="B-wrote",
                refresh_token="B-wrote",
                access_expires_at=access_exp,
                refresh_expires_at=refresh_exp,
                oa_secret_key="s",
            )

    task_a = asyncio.create_task(writer_a())
    task_b = asyncio.create_task(writer_b())
    await a_locked.wait()
    # B đang chờ lock; giải phóng A
    a_may_commit.set()
    await asyncio.gather(task_a, task_b)

    # Kết quả cuối = B (writer sau khi lấy lock)
    async with session_factory() as s:
        repo = ZaloOATokenRepo(s)
        row = await repo.get_by_shop("shop-race")
        assert row is not None
        assert row.access_token == "B-wrote"
        assert row.refresh_token == "B-wrote"
