"""Tầng 2 Phase 2.2 · assistant_rate_limit — fixed window QPM per-user.

Cover:
- Dưới limit ⇒ True mọi lần.
- Đạt limit ⇒ lần thứ N+1 = False.
- Isolation user_id — hai user không đụng bucket.
- D4 fail-open: Redis lỗi ⇒ True (allow).
- TTL 60s: window tự dọn (verify TTL sau lần acquire đầu).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from redis.exceptions import RedisError

from agent.assistant_rate_limit import _WINDOW_SECONDS, _key, try_acquire


@pytest.fixture
async def redis() -> fakeredis.aioredis.FakeRedis:
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


@pytest.mark.asyncio
async def test_try_acquire_allows_under_limit(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """limit=5, gọi 5 lần trong cùng phút ⇒ tất cả True."""
    for _ in range(5):
        assert await try_acquire(redis, "user-a", limit_qpm=5) is True


@pytest.mark.asyncio
async def test_try_acquire_blocks_at_limit(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Lần thứ 6 khi limit=5 ⇒ False (vượt)."""
    for _ in range(5):
        await try_acquire(redis, "user-a", limit_qpm=5)
    assert await try_acquire(redis, "user-a", limit_qpm=5) is False


@pytest.mark.asyncio
async def test_try_acquire_blocks_repeatedly_when_over(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Vượt limit rồi ⇒ mọi request tiếp theo trong phút = False. Không có 'quota trả về'."""
    for _ in range(3):
        await try_acquire(redis, "user-a", limit_qpm=2)
    # Ba lần đã vượt (2 allow + 1 deny + 1 deny). Kiểm 2 request nữa vẫn False.
    assert await try_acquire(redis, "user-a", limit_qpm=2) is False
    assert await try_acquire(redis, "user-a", limit_qpm=2) is False


@pytest.mark.asyncio
async def test_different_users_have_separate_buckets(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """User A hết quota KHÔNG ảnh hưởng user B."""
    for _ in range(3):
        await try_acquire(redis, "user-a", limit_qpm=3)
    assert await try_acquire(redis, "user-a", limit_qpm=3) is False
    # User B mới toanh — vẫn allow.
    assert await try_acquire(redis, "user-b", limit_qpm=3) is True


@pytest.mark.asyncio
async def test_try_acquire_sets_ttl_60s(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Lần acquire đầu phải set TTL — bucket tự dọn sau 60s (không cần cleanup job)."""
    await try_acquire(redis, "user-t", limit_qpm=10)
    ttl = await redis.ttl(_key("user-t"))
    assert 0 < ttl <= _WINDOW_SECONDS


@pytest.mark.asyncio
async def test_try_acquire_fail_open_returns_true_on_redis_error() -> None:
    """D4 tường minh · Redis chớp ⇒ allow (True). Cost > UX khi Redis down.

    Nếu ai đó refactor và đổi thành fail-closed ('an toàn hơn'), test này đỏ — ép đọc
    lại D4 trước khi land."""
    # Mock đúng dáng redis-py: pipeline() sync, incr/expire sync (queue), execute async.
    redis_mock = MagicMock()
    pipe_mock = MagicMock()
    pipe_mock.execute = AsyncMock(side_effect=RedisError("connection lost"))
    redis_mock.pipeline = MagicMock(return_value=pipe_mock)
    assert await try_acquire(redis_mock, "user-x", limit_qpm=1) is True
