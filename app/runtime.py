"""Wiring dùng chung cho các entrypoint (A4 — I1 tách process).

Ba entrypoint (`main_ohana_ai`, `main_seller`, `worker_seller`) chạy CÙNG codebase
nhưng KHÁC process, mỗi process một `DATABASE_URL` trỏ một role Postgres riêng
(svc_ohana_ai / svc_seller — docs/adopt-plan.md §3). Module này giữ phần lặp lại
giữa các app: logging setup + CSRF middleware. `app/main.py` (combined, dev-only)
cũng dùng chung để ba nơi không trôi khỏi nhau.
"""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response as StarletteResponse

from auth.identity import CSRF_COOKIE_NAME, CSRF_HEADER_NAME


def setup_logging() -> None:
    """Uvicorn cấu hình logger CỦA NÓ (`uvicorn.*`) rồi để root KHÔNG có handler và mức
    mặc định WARNING — mọi `logger.info(...)` của app bị NUỐT im lặng khi chạy thật.

    Đã cháy thật (2026-07-19): G1 yêu cầu log `model/token_in/.../shop_id` mỗi request
    chat. Test dùng `caplog.at_level(logging.INFO)` — pytest TỰ ÉP mức, nên test xanh —
    nhưng server thật không in một dòng nào. Bài học: caplog chứng minh "code có gọi
    logger", KHÔNG chứng minh "log xuất hiện ở production".

    `force=True` vì uvicorn đã chạy dictConfig trước khi import module app; không có nó
    thì basicConfig thấy root đã được đụng tới và lặng lẽ không làm gì.
    """
    logging.basicConfig(
        level=os.environ.get("OHANA_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )


# CSRF (double-submit cookie). Chỉ check request mutating dưới /api. `/api/mock/authorize`
# và `/api/mock/authorize_user` (P2.4a, Tầng 2) miễn: cả hai là route bootstrap MINT ra
# session (và chính CSRF cookie) — chưa có session nào cho một forged cross-site POST cưỡi
# lên, và đòi header ở đây làm route không gọi được từ browser sạch cookie.
#
# Cháy thật 2026-08-04: F1 thêm `authorize_user` mà quên thêm vào set này — route mint
# CHÍNH nó bị middleware nó cần miễn chặn 403 `csrf_check_failed` (F1's own unit test không
# bắt được vì `_make_app()` ở đó chỉ mount router trần, không gắn `install_csrf`). Lý do
# miễn giống hệt route kia, không phải trường hợp riêng — khi thêm route mint thứ ba thì
# soi lại comment này trước, đừng chỉ thêm route rồi test router trần.
_CSRF_EXEMPT_PATHS = {"/api/mock/authorize", "/api/mock/authorize_user"}
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def install_csrf(app: FastAPI) -> None:
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
