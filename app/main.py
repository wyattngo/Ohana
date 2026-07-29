"""FastAPI entrypoint — GD0.5 adds `/api/*` (spec 01 backend, mounted here for the first
time) and the built SPA shell at `/` (spec 04 Phase P0). Router construction stays
factory-based (`build_router(...)`) to match the existing `api/inbox.py` shape — this file
is the ONLY place that wires concrete dependencies (session factory, identity dependency)
into those factories.

Mount order matters: API routers are included BEFORE the `web/dist` static mount. Starlette
matches routes in registration order and `StaticFiles(html=True)` at `/` is a catch-all — if
it were mounted first, it would shadow every `/api/*` request.

`api/inbox.py` and `api/mock_auth.py` are mounted since P0. `api/admin.py` is mounted here as
of spec 04 Phase P2, gated by `auth.identity.require_admin` on its one route — it never lands
unguarded (P0's note above was the reason it waited). The embedder wired in is
`api/admin.py`'s `default_embedder()`, a deterministic GD0.5 placeholder — see that function's
docstring for why the real `agent/providers/openai_embedder.OpenAIEmbedder` isn't used yet (no
`app/config.py` exists anywhere in this repo). `api/webhook.py` is intentionally NOT mounted:
`agent/drafter.py::LLMDrafter` shipped in spec 13, so the Drafter contract has a real
implementation — but mounting the webhook opens the customer-inbound path, which requires
Zalo signature-verify + creds (`GD0-ZALO`, PRE-004, blocked on Tân) and starts the PDPL 60-day
clock (workflow §2.5, still no legal owner). `LLMDrafter` gets constructed in spec 15 P3 and
proven end-to-end via `MockZaloSender` integration test; the `include_router(webhook)` line
belongs to `GD0-ZALO` after those two blockers clear.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response as StarletteResponse

from api.admin import build_router as build_admin_router
from api.admin import default_embedder
from api.chat import build_router as build_chat_router
from api.inbox import build_router as build_inbox_router
from api.mock_auth import build_router as build_mock_auth_router
from auth.identity import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    build_active_shop_dep,
    build_admin_dep,
)
from db.session import make_session_factory

# ---- Logging ------------------------------------------------------------------------------
# Uvicorn cấu hình logger CỦA NÓ (`uvicorn.*`) rồi để root KHÔNG có handler và mức mặc định
# WARNING. Nên mọi `logger.info(...)` của app bị NUỐT im lặng khi chạy thật.
#
# Đã cháy thật (2026-07-19): G1 yêu cầu log `model/token_in/token_out/latency_ms/shop_id` mỗi
# request chat. Test dùng `caplog.at_level(logging.INFO)` — pytest TỰ ÉP mức, nên test xanh —
# nhưng server thật không in một dòng nào. Phát hiện khi mở trình duyệt bấm thử rồi grep log
# không thấy gì. Bài học: caplog chứng minh "code có gọi logger", KHÔNG chứng minh "log xuất
# hiện ở production".
#
# `force=True` vì uvicorn đã chạy dictConfig trước khi import module này; không có nó thì
# basicConfig thấy root đã được đụng tới và lặng lẽ không làm gì.
logging.basicConfig(
    level=os.environ.get("OHANA_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    force=True,
)

app = FastAPI(title="Ohana AI Seller", version="0.1.0")

_session_factory = make_session_factory()

# Spec 11 S1 — MỌI cửa đều đi qua dependency có đối chiếu `shops`, không cửa nào dùng
# `identity_from_cookie` trần nữa. `identity_from_cookie` chỉ chứng minh token được ký đúng;
# nó KHÔNG chứng minh `shop_id` trỏ tới một shop có thật và còn hoạt động.
#
# ⚠️ Sót MỘT call site = còn một cửa không kiểm shop, và nó sẽ không đỏ test nào trừ khi test
# probe đúng cửa đó. Vì vậy `tests/test_shops_persona.py` probe CẢ BA (inbox / chat / admin).
_identity_dep = build_active_shop_dep(_session_factory)
_admin_dep = build_admin_dep(_session_factory)

app.include_router(
    build_inbox_router(_session_factory, _identity_dep),
    prefix="/api",
)
app.include_router(build_mock_auth_router(), prefix="/api")
app.include_router(
    build_admin_router(default_embedder(), _session_factory, _admin_dep),
    prefix="/api",
)
# General Chat (spec 07 G1). No session factory and no embedder: this endpoint deliberately
# touches neither the database nor the retrieval stack — see `api/chat.py`'s module docstring
# for why that isolation is the point, not an omission. The Together client is built lazily
# inside the router's dependency, so a missing TOGETHER_API_KEY breaks /api/chat only rather
# than preventing this module from importing at all.
app.include_router(build_chat_router(_identity_dep), prefix="/api")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---- CSRF (double-submit cookie) ----------------------------------------------------------
# Only state-mutating requests under /api are checked. `/api/mock/authorize` is exempt: it's
# the bootstrap route that MINTS the session (and the CSRF cookie itself) — there is no
# session yet for a forged cross-site POST to ride on, and requiring the header here would
# make the route uncallable from a fresh browser with no cookies at all. The routes this
# actually protects are `POST /api/inbox/{id}/approve|reject`, mounted in Phase P1.
_CSRF_EXEMPT_PATHS = {"/api/mock/authorize"}
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def enforce_csrf_double_submit(
    request: Request, call_next: RequestResponseEndpoint
) -> StarletteResponse:
    if request.method not in _CSRF_SAFE_METHODS and request.url.path not in _CSRF_EXEMPT_PATHS:
        cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
        header_token = request.headers.get(CSRF_HEADER_NAME)
        if (
            not cookie_token
            or not header_token
            or not secrets.compare_digest(cookie_token, header_token)
        ):
            return JSONResponse(status_code=403, content={"detail": "csrf_check_failed"})
    return await call_next(request)


# ---- Static SPA shell (spec 04 Phase P0) --------------------------------------------------
# Built via `cd web && pnpm install && pnpm build` — NOT run automatically here. Mounted LAST
# (see module docstring) and only if the build output exists, so a fresh checkout that
# hasn't run the Node build yet fails loudly per-request (404) rather than at import time.
_WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if _WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=_WEB_DIST, html=True), name="web-spa")
