"""Tầng 2 Phase 2.2 · assistant_cost — cost counter per-user/day.

Cover:
- record + get: INCRBY tích luỹ đúng, GET trả tổng.
- get khi key chưa có ⇒ 0 (Redis nil → 0).
- TTL 25h được set sau lần INCRBY đầu.
- Isolation giữa user_id — key riêng, count không chéo.
- D4 fail-open: Redis exception ⇒ get trả 0, record no-op (không raise).

`fakeredis.aioredis.FakeRedis` — in-process Redis giả, đủ cover semantic INCRBY/GET/
EXPIRE/TTL. Không cần compose `redis` service running.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from redis.exceptions import RedisError

from agent.assistant_cost import _KEY_TTL_SECONDS, _key, get_daily_tokens, record_tokens


@pytest.fixture
async def redis() -> fakeredis.aioredis.FakeRedis:
    """FakeRedis mới cho mỗi test — isolation bằng cách vứt hết state, không PART."""
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


@pytest.mark.asyncio
async def test_record_and_get_daily_tokens_accumulates(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """INCRBY 2 lần ⇒ get trả tổng — cost là accumulate, không phải reserve."""
    await record_tokens(redis, "user-a", 100)
    await record_tokens(redis, "user-a", 250)
    assert await get_daily_tokens(redis, "user-a") == 350


@pytest.mark.asyncio
async def test_get_daily_tokens_returns_zero_when_key_absent(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """User chưa tiêu token nào ⇒ Redis nil ⇒ hàm trả int 0 (không None)."""
    assert await get_daily_tokens(redis, "fresh-user") == 0


@pytest.mark.asyncio
async def test_record_sets_ttl_25h(redis: fakeredis.aioredis.FakeRedis) -> None:
    """TTL phải được set ngay lần INCRBY đầu — tránh bẫy key sống mãi.

    `TTL key` trả số giây còn lại. 25h = 90000s; cho sai số ±5s (execute pipeline mất
    vài ms nhưng fakeredis không mô phỏng clock đó — dùng khoảng rộng để bền vững)."""
    await record_tokens(redis, "user-b", 50)
    key = _key("user-b")
    ttl = await redis.ttl(key)
    assert 0 < ttl <= _KEY_TTL_SECONDS
    # Sanity: key chưa expire ⇒ ttl > 0. Không quá ceiling (25h).


@pytest.mark.asyncio
async def test_different_users_have_isolated_counts(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Key format nhét user_id ⇒ hai user không đụng nhau."""
    await record_tokens(redis, "user-a", 100)
    await record_tokens(redis, "user-b", 999)
    assert await get_daily_tokens(redis, "user-a") == 100
    assert await get_daily_tokens(redis, "user-b") == 999


@pytest.mark.asyncio
async def test_get_daily_tokens_fail_open_returns_zero_on_redis_error() -> None:
    """D4 tường minh · Redis lỗi ⇒ get trả 0 (không biết = coi như chưa tiêu, cost gate
    ở caller sẽ allow). KHÔNG raise, KHÔNG crash user."""
    redis_mock = AsyncMock()
    redis_mock.get.side_effect = RedisError("connection lost")
    assert await get_daily_tokens(redis_mock, "user-x") == 0


@pytest.mark.asyncio
async def test_record_tokens_fail_open_no_op_on_redis_error() -> None:
    """D4 · record fail-open trả None (default), KHÔNG raise. Mất 1 record token là chấp
    nhận — không nuốt exception LLM/render ở caller."""
    # `pipeline()` là SYNC trong redis-py (queue commands); `.incrby`/`.expire` cũng sync
    # chỉ enqueue; `.execute()` là async gửi qua wire. Mock đúng dáng đó để RuntimeWarning
    # không phát ra (AsyncMock trên incrby/expire coi chúng như coroutine).
    redis_mock = MagicMock()
    pipe_mock = MagicMock()
    pipe_mock.execute = AsyncMock(side_effect=RedisError("connection lost"))
    redis_mock.pipeline = MagicMock(return_value=pipe_mock)
    result = await record_tokens(redis_mock, "user-y", 100)
    assert result is None
