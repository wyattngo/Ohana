"""Tầng 2 Phase 2.2 · redis_client + fail_open — gate hạ tầng Redis luồng A.

Cover:
- `make_redis_pool` build từ URL không raise, trả ConnectionPool.
- `fail_open`:
  - happy path pass-through.
  - RedisError → default + log WARNING event=redis_fail_open.
  - ConnectionError → default (Python built-in, có thể raise trước wrap).
  - asyncio.TimeoutError → default (pool exhausted).
  - NON-Redis errors (ValueError, TypeError, ...) → RE-RAISE (kỷ luật KHÔNG catch chung).

Test dùng `fakeredis.aioredis.FakeRedis` để không cần Redis service running khi chạy
unit tests (CI + máy dev không docker-compose up cũng chạy được).
"""

from __future__ import annotations

import logging

import pytest
from redis.asyncio.connection import ConnectionPool
from redis.exceptions import RedisError

from agent.redis_client import fail_open, get_redis, make_redis_pool


def test_make_redis_pool_returns_connection_pool() -> None:
    """Factory chấp nhận URL hợp lệ, không raise, trả ConnectionPool instance."""
    pool = make_redis_pool("redis://localhost:6379/0")
    assert isinstance(pool, ConnectionPool)


def test_get_redis_wraps_pool() -> None:
    """`get_redis(pool)` trả `Redis` client bind vào cùng pool — không tạo pool thứ hai."""
    pool = make_redis_pool("redis://localhost:6379/0")
    client = get_redis(pool)
    assert client.connection_pool is pool


@pytest.mark.asyncio
async def test_fail_open_returns_value_when_op_ok() -> None:
    """Happy path: hàm không raise ⇒ decorator pass-through kết quả gốc."""

    @fail_open(default=99)
    async def _op() -> int:
        return 42

    assert await _op() == 42


@pytest.mark.asyncio
async def test_fail_open_returns_default_on_redis_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D4 gate · RedisError ⇒ decorator trả default + log WARNING structured.

    Log format `event=redis_fail_open op=... err=...` là hợp đồng với log ingest —
    filter được ngay ở Langfuse/Loki. Đổi format ở đây phải cập nhật dashboard."""

    @fail_open(default=42)
    async def _op() -> int:
        raise RedisError("connection reset")

    with caplog.at_level(logging.WARNING, logger="agent.redis_client"):
        result = await _op()

    assert result == 42
    assert any("event=redis_fail_open" in rec.message for rec in caplog.records)
    assert any("op=_op" in rec.message for rec in caplog.records)
    assert any("err=RedisError" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_fail_open_catches_connection_error() -> None:
    """Python built-in `ConnectionError` cũng phải bắt — SDK redis có thể raise trước wrap."""

    @fail_open(default="fallback")
    async def _op() -> str:
        raise ConnectionError("refused")

    assert await _op() == "fallback"


@pytest.mark.asyncio
async def test_fail_open_catches_timeout() -> None:
    """Pool exhausted / socket timeout ⇒ fail-open, không escalate.

    Python 3.11+ hợp nhất `asyncio.TimeoutError` là alias của `builtins.TimeoutError` —
    dùng built-in name cho gọn (UP041)."""

    @fail_open(default=0)
    async def _op() -> int:
        raise TimeoutError

    assert await _op() == 0


@pytest.mark.asyncio
async def test_fail_open_does_not_swallow_non_redis_errors() -> None:
    """Bug trong code mình (ValueError, TypeError, ...) phải RE-RAISE — kỷ luật KHÔNG
    catch `Exception` chung. Đây là chỗ `alert_service` từng cháy: bắt hết = che bẫy."""

    @fail_open(default=0)
    async def _op() -> int:
        raise ValueError("bug in call-site")

    with pytest.raises(ValueError, match="bug in call-site"):
        await _op()


@pytest.mark.asyncio
async def test_fail_open_does_not_swallow_type_error() -> None:
    """TypeError cùng bài — không phải Redis lỗi, không được ăn."""

    @fail_open(default=None)
    async def _op() -> None:
        raise TypeError("wrong arg count")

    with pytest.raises(TypeError):
        await _op()
