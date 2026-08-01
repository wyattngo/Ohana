"""Phase 2.4a · /mock/authorize_user — mint user Tầng 2 token (dev-only).

Cover:
- 404 outside dev (fail-closed như `/mock/authorize`).
- 422 invalid tier.
- Happy path: mint token verify được bằng `verify_user_token` với `get_jwt_secret()`.
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
