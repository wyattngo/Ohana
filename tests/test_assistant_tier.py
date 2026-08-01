"""Phase 2.4a · assistant_tier — free/pro tier gate + consume rate + cost primitives.

Cover:
- allow khi under limits (free tier, 0 usage).
- deny rate FIRST (INCR trước GET).
- deny cost cap khi rate còn quota nhưng token đã hết.
- pro tier có limit cao hơn (verify TIER_LIMITS map đúng).
- unknown tier ⇒ deny reason="unknown_tier" (belt-and-braces).
- D4 fail-open: Redis down ⇒ allow (propagate qua try_acquire + get_daily_tokens).
- daily_tokens_used echoed trong verdict cho router/UI.

`fakeredis.aioredis.FakeRedis` — same pattern P2.2 tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from redis.exceptions import RedisError

from agent.assistant_tier import TIER_LIMITS, TierVerdict, check_and_reserve
from auth.user_identity import UserIdentity


@pytest.fixture
async def redis() -> fakeredis.aioredis.FakeRedis:
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


@pytest.mark.asyncio
async def test_check_and_reserve_allows_when_under_limits(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Free tier, chưa dùng gì ⇒ allow, reason rỗng, used = 0."""
    identity = UserIdentity(user_id="u-1", tier="free")
    verdict = await check_and_reserve(redis, identity)
    assert verdict == TierVerdict(allowed=True, reason="", daily_tokens_used=0)


@pytest.mark.asyncio
async def test_check_and_reserve_denies_rate_limit_at_threshold(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Free tier qpm=10 ⇒ lần thứ 11 trong phút deny reason=rate_limit_exceeded."""
    identity = UserIdentity(user_id="u-1", tier="free")
    for _ in range(10):
        v = await check_and_reserve(redis, identity)
        assert v.allowed is True
    v = await check_and_reserve(redis, identity)
    assert v.allowed is False
    assert v.reason == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_check_and_reserve_denies_daily_cost_cap(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Rate còn quota (free tier lần đầu) NHƯNG counter cost đã = daily_tokens cap
    ⇒ deny reason=daily_cost_cap_exceeded."""
    identity = UserIdentity(user_id="u-2", tier="free")
    # Pre-set cost counter đầy ngưỡng cap. Key format khớp assistant_cost:
    # `cost:user:{user_id}:{YYYY-MM-DD}` — dùng module để tránh drift.
    from agent.assistant_cost import _key as cost_key

    await redis.set(cost_key("u-2"), TIER_LIMITS["free"].daily_tokens)

    v = await check_and_reserve(redis, identity)
    assert v.allowed is False
    assert v.reason == "daily_cost_cap_exceeded"
    assert v.daily_tokens_used == TIER_LIMITS["free"].daily_tokens


@pytest.mark.asyncio
async def test_check_and_reserve_daily_tokens_used_echoed_when_allow(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Verdict allow phải kèm `daily_tokens_used` cho router response/UI progress."""
    identity = UserIdentity(user_id="u-3", tier="free")
    from agent.assistant_cost import _key as cost_key

    await redis.set(cost_key("u-3"), 1000)

    v = await check_and_reserve(redis, identity)
    assert v.allowed is True
    assert v.daily_tokens_used == 1000


@pytest.mark.asyncio
async def test_check_and_reserve_rate_checked_before_cost(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Khi cả rate VÀ cost cùng vượt: reason = rate (INCR before GET). Verify thứ tự."""
    identity = UserIdentity(user_id="u-4", tier="free")
    # Đẩy cost trên cap trước.
    from agent.assistant_cost import _key as cost_key

    await redis.set(cost_key("u-4"), TIER_LIMITS["free"].daily_tokens + 1)
    # Đẩy rate trên qpm cap.
    for _ in range(10):
        await check_and_reserve(redis, identity)  # dùng hết 10 slot; nhưng 5 slot đầu
        # trong đó verdict là daily_cost_cap_exceeded (cost đã vượt). Cứ INCR liên tục.

    # Lần thứ 11 (đã 10 slot rate + vượt cost): rate INCR trước → rate vượt ⇒ deny rate.
    v = await check_and_reserve(redis, identity)
    assert v.allowed is False
    assert v.reason == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_pro_tier_has_higher_rate_limit(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Pro qpm=60; free qpm=10. 20 call pro vẫn allow; 20 call free deny từ 11."""
    pro = UserIdentity(user_id="u-pro", tier="pro")
    free = UserIdentity(user_id="u-free", tier="free")

    for _ in range(20):
        v = await check_and_reserve(redis, pro)
        assert v.allowed is True

    for i in range(20):
        v = await check_and_reserve(redis, free)
        if i < 10:
            assert v.allowed is True
        else:
            assert v.allowed is False
            assert v.reason == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_pro_tier_has_higher_daily_cost_cap(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Free cap 50k, pro cap 500k. Set counter 100k → free deny, pro allow."""
    from agent.assistant_cost import _key as cost_key

    await redis.set(cost_key("u-a"), 100_000)
    await redis.set(cost_key("u-b"), 100_000)

    free_verdict = await check_and_reserve(redis, UserIdentity(user_id="u-a", tier="free"))
    pro_verdict = await check_and_reserve(redis, UserIdentity(user_id="u-b", tier="pro"))
    assert free_verdict.allowed is False
    assert free_verdict.reason == "daily_cost_cap_exceeded"
    assert pro_verdict.allowed is True


@pytest.mark.asyncio
async def test_unknown_tier_returns_denied(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Tier không có trong TIER_LIMITS (bypass verify — direct construct) ⇒ deny.
    Belt-and-braces: verify đã whitelist; guard này bắt drift verify/gate."""
    # Bypass constructor validation — UserIdentity là dc, không validate tier ở đó.
    identity = UserIdentity(user_id="u-x", tier="enterprise")
    v = await check_and_reserve(redis, identity)
    assert v.allowed is False
    assert v.reason == "unknown_tier"
    assert v.daily_tokens_used == 0


@pytest.mark.asyncio
async def test_fail_open_allows_when_redis_down() -> None:
    """D4 · Redis exception ở try_acquire + get_daily_tokens ⇒ cả hai fail-open (True/0)
    ⇒ gate allow. Cost > UX khi Redis lỗi."""
    redis_mock = MagicMock()
    pipe_mock = MagicMock()
    pipe_mock.execute = AsyncMock(side_effect=RedisError("connection lost"))
    redis_mock.pipeline = MagicMock(return_value=pipe_mock)
    redis_mock.get = AsyncMock(side_effect=RedisError("connection lost"))

    identity = UserIdentity(user_id="u-z", tier="free")
    v = await check_and_reserve(redis_mock, identity)
    assert v.allowed is True
    assert v.reason == ""
    assert v.daily_tokens_used == 0


@pytest.mark.asyncio
async def test_different_users_have_isolated_rate_buckets(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """User A hết quota rate không ảnh hưởng user B (cùng bài rate_limit test isolation)."""
    a = UserIdentity(user_id="u-A", tier="free")
    b = UserIdentity(user_id="u-B", tier="free")
    for _ in range(11):
        await check_and_reserve(redis, a)
    v_a = await check_and_reserve(redis, a)
    v_b = await check_and_reserve(redis, b)
    assert v_a.allowed is False
    assert v_b.allowed is True
