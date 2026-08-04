"""Phase 2.4a · /mock/authorize_user — mint user Tầng 2 token (dev-only).

Cover:
- 404 outside dev (fail-closed như `/mock/authorize`).
- 422 invalid tier.
- Happy path: mint token verify được bằng `verify_user_token` với `get_jwt_secret()`.
- CSRF exemption qua APP THẬT (regression — xem `test_authorize_user_is_csrf_exempt...`
  bên dưới cho lý do file này từng để lọt bug).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.mock_auth import build_router
from auth.identity import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, get_jwt_secret
from auth.user_identity import UserIdentity, verify_user_token


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(build_router(), prefix="/api")
    return app


def test_authorize_user_404_outside_dev(monkeypatch) -> None:
    """OHANA_ENV unset ⇒ 404 (fail-closed — deploy quên set env KHÔNG nên lộ mint)."""
    monkeypatch.delenv("OHANA_ENV", raising=False)
    with TestClient(_make_app()) as client:
        resp = client.post("/api/mock/authorize_user?tier=free")
    assert resp.status_code == 404


def test_authorize_user_422_invalid_tier(monkeypatch) -> None:
    """Tier ngoài whitelist ({"free","pro"}) ⇒ 422."""
    monkeypatch.setenv("OHANA_ENV", "dev")
    monkeypatch.setenv("OHANA_JWT_SECRET", "test-secret-p24a")
    with TestClient(_make_app()) as client:
        resp = client.post("/api/mock/authorize_user?tier=premium")
    assert resp.status_code == 422
    assert resp.json() == {"detail": "invalid_tier"}


def test_authorize_user_mints_valid_token(monkeypatch) -> None:
    """Dev + tier=pro ⇒ 200, cookie set, token verify OK và ra UserIdentity đúng."""
    monkeypatch.setenv("OHANA_ENV", "dev")
    monkeypatch.setenv("OHANA_JWT_SECRET", "test-secret-p24a")

    with TestClient(_make_app()) as client:
        resp = client.post("/api/mock/authorize_user?tier=pro")

    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "user"
    assert body["tier"] == "pro"
    assert body["user_id"] == "dev-user-t2-001"

    # Cookies được set — cả session (httpOnly) và csrf (JS-readable).
    session_cookie = resp.cookies.get(SESSION_COOKIE_NAME)
    csrf_cookie = resp.cookies.get(CSRF_COOKIE_NAME)
    assert session_cookie is not None
    assert csrf_cookie is not None

    # Token verify được với secret hiện tại ⇒ ra UserIdentity đúng.
    identity = verify_user_token(session_cookie, secret=get_jwt_secret())
    assert identity == UserIdentity(user_id="dev-user-t2-001", tier="pro")


def test_authorize_user_default_tier_free(monkeypatch) -> None:
    """Không truyền query `tier` ⇒ default `free` (tiện dev; verify token có tier)."""
    monkeypatch.setenv("OHANA_ENV", "dev")
    monkeypatch.setenv("OHANA_JWT_SECRET", "test-secret-p24a")

    with TestClient(_make_app()) as client:
        resp = client.post("/api/mock/authorize_user")

    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "free"
    session_cookie = resp.cookies.get(SESSION_COOKIE_NAME)
    identity = verify_user_token(session_cookie, secret=get_jwt_secret())
    assert identity.tier == "free"


def test_authorize_user_is_csrf_exempt_through_the_real_app(monkeypatch) -> None:
    """Regression cháy thật 2026-08-04: mọi test ở trên dựng `_make_app()` — router trần,
    KHÔNG gắn `install_csrf` — nên cả bốn test kia xanh trong khi route thật 403 ngay lượt
    gọi đầu tiên từ browser sạch cookie. `app/runtime.py`'s CSRF double-submit middleware
    chặn MỌI POST không có cặp cookie/header khớp, và route mint không có cookie nào ở
    lượt gọi đầu (nó CHÍNH LÀ nơi sinh ra cookie CSRF) — `_CSRF_EXEMPT_PATHS` quên thêm
    `/api/mock/authorize_user` khi F1 thêm route này (chỉ có `/api/mock/authorize` từ P0).

    Test này đi qua `app.main:app` THẬT (middleware đầy đủ), gọi với ZERO cookie — đúng
    hệt trạng thái một tab trình duyệt mới mở — và đòi 200, không phải 403
    `csrf_check_failed`. Bug này gate-được ở đây vì dùng app thật; không gate được ở bốn
    test phía trên vì chúng cố tình bare-router (đơn vị, không phải tích hợp)."""
    monkeypatch.setenv("OHANA_ENV", "dev")
    monkeypatch.setenv("OHANA_JWT_SECRET", "test-secret-p24a")
    from app.main import app

    with TestClient(app) as client:
        resp = client.post("/api/mock/authorize_user?tier=free")

    assert resp.status_code == 200, (
        f"route mint bị CSRF middleware tự chặn chính nó (status={resp.status_code}, "
        f"body={resp.text!r}) — thiếu trong _CSRF_EXEMPT_PATHS (app/runtime.py)"
    )
