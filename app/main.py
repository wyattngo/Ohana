"""FastAPI entrypoint COMBINED — dev-only kể từ A4.

⚠️ A4 (I1) tách hai luồng ra hai process: `app/main_ohana_ai.py` (luồng A, role
svc_ohana_ai) và `app/main_seller.py` (luồng B, role svc_seller) + `app/worker_seller.py`.
File này giữ nguyên cho local dev một-process và cho tới khi compose có service riêng
(adopt-plan §5) — nó chạy CẢ HAI luồng chung một `DATABASE_URL`, tức KHÔNG có ranh giới
I1/I2 nào; đừng deploy nó.

GD0.5 adds `/api/*` (spec 01 backend, mounted here for the first
time) and the built SPA shell at `/` (spec 04 Phase P0). Router construction stays
factory-based (`build_router(...)`) to match the existing `api/inbox.py` shape — entrypoint
là nơi DUY NHẤT wire concrete dependencies (session factory, identity dependency)
into those factories.

Mount order matters: API routers are included BEFORE the `web/dist` static mount. Starlette
matches routes in registration order and `StaticFiles(html=True)` at `/` is a catch-all — if
it were mounted first, it would shadow every `/api/*` request.

`api/inbox.py` and `api/mock_auth.py` are mounted since P0. `api/admin.py` is mounted here as
of spec 04 Phase P2, gated by `auth.identity.require_admin` on its one route — it never lands
unguarded (P0's note above was the reason it waited). The embedder wired in comes from
`agent/embedder.py::default_embedder()` (the I5b seam) — env-selecting, real adapter when a
key is configured, dev fallback otherwise; see that function's docstring.
`api/webhook.py` is intentionally NOT mounted:
`agent/drafter.py::LLMDrafter` shipped in spec 13, so the Drafter contract has a real
implementation — but mounting the webhook opens the customer-inbound path, which requires
Zalo signature-verify + creds (`GD0-ZALO`, PRE-004, blocked on Tân) and starts the PDPL 60-day
clock (workflow §2.5, still no legal owner). `LLMDrafter` gets constructed in spec 15 P3 and
proven end-to-end via `MockZaloSender` integration test; the `include_router(webhook)` line
belongs to `GD0-ZALO` after those two blockers clear.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from agent.embedder import default_embedder
from api.admin import build_router as build_admin_router
from api.chat import build_router as build_chat_router
from api.inbox import build_router as build_inbox_router
from api.mock_auth import build_router as build_mock_auth_router
from app.runtime import install_csrf, setup_logging
from auth.identity import build_active_shop_dep, build_admin_dep
from db.session import make_session_factory

# Logging + CSRF chuyển sang app/runtime.py ở A4 — ba entrypoint dùng chung, xem
# docstring bên đó cho bài học caplog/force=True và contract double-submit.
setup_logging()

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


install_csrf(app)


# ---- Static SPA shell (spec 04 Phase P0) --------------------------------------------------
# Built via `cd web && pnpm install && pnpm build` — NOT run automatically here. Mounted LAST
# (see module docstring) and only if the build output exists, so a fresh checkout that
# hasn't run the Node build yet fails loudly per-request (404) rather than at import time.
_WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if _WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=_WEB_DIST, html=True), name="web-spa")
