"""Entrypoint luồng A — Ohana AI chat (A4 · I1 tách process).

CHỈ mount `api/chat` (+ `api/mock_auth` để mint session dev). KHÔNG mount inbox,
admin, webhook — luồng B sống ở `app/main_seller.py`, process khác.

I1 được cưỡng chế hai tầng:
- import: contract I1 trong pyproject liệt module này vào source_modules — import
  bất cứ gì thuộc luồng B (api.webhook, api.inbox, agent.drafter, channels, bridge)
  là `lint-imports` đỏ.
- DB: chạy với `DATABASE_URL` trỏ role `svc_ohana_ai` — zero quyền trên `public`
  (I2), nên kể cả code có bug cũng không đọc được dữ liệu shop: permission denied
  ở tầng Postgres, không phải ở tầng review.

    DATABASE_URL="postgresql+psycopg://svc_ohana_ai:$SVC_A_PW@localhost:5432/ohana" \\
        uvicorn app.main_ohana_ai:app --port 8001

Auth: a2 mở `SELECT public.shops` cho svc_ohana_ai (`shops` đóng vai registry
`core.account_shop` §8.4) — `build_active_shop_dep` chạy được. Mock login ở dev
KHÔNG seed được fixture shop dưới role này (a2 cố ý không cho INSERT) — seed bằng
app seller/combined trước; `mock_auth` log warning chỉ thẳng chỗ đó.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent.redis_client import make_redis_pool
from api.chat import build_router as build_chat_router
from api.mock_auth import build_router as build_mock_auth_router
from app.config import get_settings
from app.runtime import install_csrf, setup_logging
from auth.identity import build_active_shop_dep
from db.session import make_session_factory

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Redis pool (Tầng 2 Phase 2.2, D3) — startup dựng, shutdown đóng.

    Pool đặt trên `app.state.redis_pool` để Phase 2.4 router chat (`Depends(get_redis)`)
    lấy được từ `request.app.state`. Không expose global vì (a) test cần isolation
    per-fixture, (b) singleton module-scope là bẫy khi có nhiều process (uvicorn --workers).

    Không cần healthcheck ở startup — D4 fail-open: Redis chớp ⇒ mất gate tạm, không
    block boot. Redis chưa sẵn ⇒ ConnectionPool build vẫn OK (lazy connect ở op đầu).
    """
    settings = get_settings()
    app.state.redis_pool = make_redis_pool(settings.redis_url)
    try:
        yield
    finally:
        await app.state.redis_pool.aclose()


app = FastAPI(title="Ohana AI", version="0.1.0", lifespan=lifespan)

_session_factory = make_session_factory()
_identity_dep = build_active_shop_dep(_session_factory)

app.include_router(build_mock_auth_router(), prefix="/api")
app.include_router(build_chat_router(_identity_dep), prefix="/api")

install_csrf(app)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
