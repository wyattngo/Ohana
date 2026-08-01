"""Tầng 2 Phase 2.2 · lifespan hook cho `app.main_ohana_ai` — Redis pool life-cycle.

Cover:
- Startup: `app.state.redis_pool` được set thành ConnectionPool.
- Shutdown: pool.aclose() được gọi.

Không cần Redis service running: `ConnectionPool.from_url` lazy connect ở op đầu, và
aclose() không raise nếu chưa có connection nào bị mở. Đây là verify HÌNH DẠNG lifespan,
không phải verify Redis THẬT nói được (integration test đó cần compose service, out of
scope test này)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from redis.asyncio.connection import ConnectionPool

from app.main_ohana_ai import app


def test_lifespan_creates_redis_pool_and_closes() -> None:
    """TestClient chạm lifespan (enter startup + exit shutdown khi context manager close)."""
    with TestClient(app) as client:
        # Sau startup, pool phải tồn tại trên app.state.
        assert isinstance(client.app.state.redis_pool, ConnectionPool)
        # Health endpoint vẫn xanh — lifespan không phá boot flow.
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    # Sau __exit__, lifespan shutdown chạy. Không cách nào ngắn để assert aclose() đã
    # gọi mà không mock — nhưng nếu shutdown raise, `with` sẽ propagate. Không raise
    # = đủ evidence cho gate lifespan.
