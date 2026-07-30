"""Entrypoint luồng B — AI Seller (A4 · I1 tách process).

Mount inbox + admin + mock_auth + SPA shell. KHÔNG mount `api/chat` — luồng A sống
ở `app/main_ohana_ai.py`, process khác. `api/webhook` vẫn CHƯA mount ở đây, cùng
lý do đã ghi ở `app/main.py`: mở đường customer-inbound cần Zalo creds (PRE-004)
và khởi động đồng hồ PDPL 60 ngày — thuộc GD0-ZALO, không thuộc A4.

Chạy với `DATABASE_URL` trỏ role `svc_seller`: SELECT/INSERT/UPDATE trên `public`,
đọc `platform` qua USAGE — không tạo bảng được (I14: chỉ `ohana_migrator` tạo bảng).

    DATABASE_URL="postgresql+psycopg://svc_seller:$SVC_B_PW@localhost:5432/ohana" \\
        uvicorn app.main_seller:app --port 8002

Wiki ingest + RAG: a2 mở SELECT/INSERT/UPDATE/DELETE trên đúng bảng
`platform.embeddings` (+ sequence) cho svc_seller — route admin ingest và đường
drafter→shop_kb→retrieval chạy được; bảng platform tương lai vẫn phải grant
tường minh (a2 cố ý không đặt default privileges ở platform cho role này).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from agent.embedder import default_embedder
from api.admin import build_router as build_admin_router
from api.inbox import build_router as build_inbox_router
from api.mock_auth import build_router as build_mock_auth_router
from app.runtime import install_csrf, setup_logging
from auth.identity import build_active_shop_dep, build_admin_dep
from db.session import make_session_factory

setup_logging()

app = FastAPI(title="Ohana AI Seller", version="0.1.0")

_session_factory = make_session_factory()
_identity_dep = build_active_shop_dep(_session_factory)
_admin_dep = build_admin_dep(_session_factory)

app.include_router(build_inbox_router(_session_factory, _identity_dep), prefix="/api")
app.include_router(build_mock_auth_router(), prefix="/api")
app.include_router(
    build_admin_router(default_embedder(), _session_factory, _admin_dep),
    prefix="/api",
)

install_csrf(app)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# SPA shell — mount CUỐI: StaticFiles(html=True) tại `/` là catch-all, mount trước
# sẽ nuốt mọi /api/*. Chỉ mount khi build output tồn tại (fail loudly per-request
# thay vì chết lúc import trên checkout chưa build web).
_WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if _WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=_WEB_DIST, html=True), name="web-spa")
