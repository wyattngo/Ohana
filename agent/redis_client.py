"""Redis client + fail-open wrapper cho Tầng 2 (ADR D3 + D4).

Hạ tầng dùng chung cho `agent/assistant_cost.py` và `agent/assistant_rate_limit.py`
(Phase 2.2). Sống trong `agent/` (luồng A); importlinter contract **I1c** cấm bridge/
channels/luồng B đụng vào — cùng bài với I1 gốc: tách process là tường lửa của I1.

**D3 · Redis-KV, KHÔNG queue.** §10 của `ohana-be-design.md` cấm Redis-làm-queue (outbox
đã là queue); Redis ở đây chỉ INCRBY/GET/EXPIRE cho counter và rate-limit. Không có
`LPUSH`/`BRPOP`/pub-sub ở scope Tầng 2 hiện tại.

**D4 · fail-open khi Redis down.** Redis chớp ⇒ mất gate tạm, KHÔNG chặn user. An toàn
trải nghiệm > an toàn cost khi Redis lỗi. Cưỡng chế bằng decorator `fail_open` — bắt
`RedisError` + `ConnectionError` + `asyncio.TimeoutError`, log WARNING structured, trả
`default`. Không catch `Exception` chung — bắt hết = che bug code mình (kỷ luật cùng bài
với `alert_service.record_provider_429`).

**Pool life-cycle.** `make_redis_pool(url)` trả `ConnectionPool` — caller (lifespan của
`main_ohana_ai`) sở hữu và `.aclose()` khi shutdown. Module này KHÔNG cache global pool
để tránh trap "test này tạo pool, test kia dùng lại pool cũ" (đã cháy ở `alert_service`
in-process counter — xem docstring ở đó).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import redis.asyncio as redis_async
from redis.asyncio.connection import ConnectionPool
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


def make_redis_pool(url: str) -> ConnectionPool:
    """Tạo `ConnectionPool` từ URL. Caller sở hữu; nhớ `await pool.aclose()` khi shutdown.

    `from_url` là chỗ duy nhất parse DSN — đổi provider (Sentinel/Cluster) sau này chỉ
    sửa hàm này, các call-site (cost/rate-limit) không đổi. `decode_responses=False` để
    caller quyết encoding: INCRBY/GET trả bytes → int cast ở call-site (tránh silent
    UTF-8 lỗi ở tầng pool).
    """
    return ConnectionPool.from_url(url, decode_responses=False)


def get_redis(pool: ConnectionPool) -> redis_async.Redis:
    """Client wrapper thin — chỉ để test mock dễ hơn (thay pool là thay client)."""
    return redis_async.Redis(connection_pool=pool)


def fail_open(
    default: T,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Decorator D4: bắt lỗi Redis + trả `default`. Không catch `Exception` chung.

    Ba loại lỗi bắt:
    - `redis.RedisError` — mọi lỗi từ Redis (network, WRONGTYPE, OOM, ...).
    - `ConnectionError` — Python built-in (redis SDK có thể raise trước khi bọc lại).
    - `TimeoutError` — built-in (Python 3.11+ hợp nhất `asyncio.TimeoutError` và
      `builtins.TimeoutError`; pool exhausted / socket timeout đều raise loại này).

    Bug ở CODE MÌNH (ValueError, TypeError, AttributeError, ...) vẫn RE-RAISE — bắt hết
    = che bẫy im lặng, và đó là cách `alert_service` từng cháy (đã học một lần, không lặp).

    Log format structured: `event=redis_fail_open op=<fn> err=<type>: <msg>`. Ai ingest
    log (Langfuse/loki) filter được ngay.
    """

    def _decorator(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(fn)
        async def _wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            try:
                return await fn(*args, **kwargs)
            except (RedisError, ConnectionError, TimeoutError) as exc:
                logger.warning(
                    "event=redis_fail_open op=%s err=%s: %s",
                    fn.__name__,
                    type(exc).__name__,
                    exc,
                )
                return default

        return _wrapper

    return _decorator
