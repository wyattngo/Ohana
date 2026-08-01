"""Phase 2.4a · UserIdentity + verify_user_token + user_identity_from_cookie.

Cover:
- verify happy path (HS256 sign + verify).
- verify reject wrong role, missing sub/tier, invalid tier value, wrong algorithm,
  tampered signature.
- cookie dep 401 khi missing / seller token / invalid.
- cookie dep happy path.

TEST-ONLY dep injection cho `user_identity_from_cookie` — không cần lifespan hay real
identity flow.
"""

from __future__ import annotations

import jwt
import pytest
from fastapi.testclient import TestClient

from auth.identity import SESSION_COOKIE_NAME
from auth.user_identity import (
    UserIdentity,
    user_identity_from_cookie,
    verify_user_token,
)

_SECRET = "test-secret-for-p24a-only"


def _mint(payload: dict, *, alg: str = "HS256", secret: str = _SECRET) -> str:
    """Helper — encode JWT với payload cho test. Không dùng `get_jwt_secret` để test
    isolation khỏi env."""
    return jwt.encode(payload, secret, algorithm=alg)


def test_verify_user_token_happy() -> None:
    """Payload đúng shape (sub + role="user" + tier=free/pro) ⇒ UserIdentity."""
    token = _mint({"sub": "user-42", "role": "user", "tier": "free"})
    identity = verify_user_token(token, secret=_SECRET)
    assert identity == UserIdentity(user_id="user-42", tier="free")


def test_verify_accepts_pro_tier() -> None:
    """Whitelist gồm cả `pro`, không chỉ `free`."""
    token = _mint({"sub": "user-42", "role": "user", "tier": "pro"})
    identity = verify_user_token(token, secret=_SECRET)
    assert identity.tier == "pro"


def test_verify_rejects_wrong_role_seller() -> None:
    """Seller token trong cookie Tầng 2 route ⇒ ValueError (route sau raise 401)."""
    token = _mint({"sub": "user-42", "role": "seller", "tier": "free"})
    with pytest.raises(ValueError, match="invalid_role"):
        verify_user_token(token, secret=_SECRET)


def test_verify_rejects_wrong_role_admin() -> None:
    """Admin token cũng bị reject — Tầng 2 chỉ nhận role `user`."""
    token = _mint({"sub": "user-42", "role": "admin", "tier": "free"})
    with pytest.raises(ValueError, match="invalid_role"):
        verify_user_token(token, secret=_SECRET)


def test_verify_rejects_missing_sub() -> None:
    """Thiếu `sub` ⇒ ValueError. Không silent default (attacker forge thiếu claim
    = attacker forge success nếu ta im lặng)."""
    token = _mint({"role": "user", "tier": "free"})
    with pytest.raises(ValueError, match="invalid_sub"):
        verify_user_token(token, secret=_SECRET)


def test_verify_rejects_missing_tier() -> None:
    """Thiếu `tier` ⇒ ValueError. KHÔNG âm thầm assign "free" (đó là leak: attacker
    strip claim → tier default rẻ hơn thật)."""
    token = _mint({"sub": "user-42", "role": "user"})
    with pytest.raises(ValueError, match="invalid_tier"):
        verify_user_token(token, secret=_SECRET)


def test_verify_rejects_invalid_tier_value() -> None:
    """Tier ngoài whitelist ({"free","pro"}) ⇒ ValueError. Thêm tier mới phải sửa cả
    `_ALLOWED_TIERS` và `TIER_LIMITS` — test này bắt drift 1 chỗ."""
    token = _mint({"sub": "user-42", "role": "user", "tier": "premium"})
    with pytest.raises(ValueError, match="invalid_tier"):
        verify_user_token(token, secret=_SECRET)


def test_verify_rejects_non_string_sub() -> None:
    """`sub` phải là str non-empty; số ⇒ reject.

    PyJWT tự validate `sub` là str ở tầng decode (`InvalidSubjectError` subclass của
    `InvalidTokenError`) — trước khi tới validator của mình. Cả hai đều đóng đúng lỗ
    hổng; test accept cả hai để không phụ thuộc chi tiết implement của jwt lib."""
    token = _mint({"sub": 42, "role": "user", "tier": "free"})
    with pytest.raises((ValueError, jwt.InvalidTokenError)):
        verify_user_token(token, secret=_SECRET)


def test_verify_rejects_wrong_algorithm() -> None:
    """Pin HS256 giữ: token ký với alg khác ⇒ InvalidAlgorithmError (subclass của
    InvalidTokenError). Đây là chỗ chặn alg-confusion bypass."""
    # None alg — jwt lib không cho encode với "none" mà không dùng token unsecured;
    # thay vào đó gửi token HS512 (khác allowed HS256).
    token = _mint({"sub": "x", "role": "user", "tier": "free"}, alg="HS512")
    with pytest.raises(jwt.InvalidTokenError):
        verify_user_token(token, secret=_SECRET)


def test_verify_rejects_tampered_signature() -> None:
    """Sửa payload sau khi ký ⇒ InvalidSignatureError. Cả secret bị đổi cũng cùng
    exception (guard chống forge)."""
    token = _mint({"sub": "user-42", "role": "user", "tier": "free"})
    with pytest.raises(jwt.InvalidTokenError):
        verify_user_token(token, secret="wrong-secret")


def _make_test_app_with_dep():
    """Mini app dùng user_identity_from_cookie như dependency thật — TestClient sẽ
    xử lý HTTPException đúng shape 401."""
    from fastapi import Depends, FastAPI

    app = FastAPI()

    # B008 workaround: Depends() ở default là idiom FastAPI hợp lệ nhưng ruff cảnh báo
    # (rule chung cho function-call defaults). Bind ra module-level cũng OK — ở test này
    # bind cục bộ trong scope hàm.
    _identity_dep = Depends(user_identity_from_cookie)

    @app.get("/whoami")
    async def whoami(identity: UserIdentity = _identity_dep) -> dict:
        return {"user_id": identity.user_id, "tier": identity.tier}

    return app


def test_user_identity_from_cookie_401_when_missing(monkeypatch) -> None:
    """No cookie ⇒ 401 `missing_session_cookie`."""
    monkeypatch.setenv("OHANA_JWT_SECRET", _SECRET)
    app = _make_test_app_with_dep()
    with TestClient(app) as client:
        resp = client.get("/whoami")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "missing_session_cookie"}


def test_user_identity_from_cookie_401_when_seller_token(monkeypatch) -> None:
    """Seller token trong cookie ⇒ 401 (same shape với missing/invalid — không leak
    'bạn đang login sai role')."""
    monkeypatch.setenv("OHANA_JWT_SECRET", _SECRET)
    seller_token = _mint({"sub": "seller-x", "role": "seller", "shop_id": "shop-1"})
    app = _make_test_app_with_dep()
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, seller_token)
        resp = client.get("/whoami")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "invalid_session_cookie"}


def test_user_identity_from_cookie_happy(monkeypatch) -> None:
    """Valid user token trong cookie ⇒ 200 với UserIdentity fields."""
    monkeypatch.setenv("OHANA_JWT_SECRET", _SECRET)
    user_token = _mint({"sub": "user-alice", "role": "user", "tier": "pro"})
    app = _make_test_app_with_dep()
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, user_token)
        resp = client.get("/whoami")
    assert resp.status_code == 200
    assert resp.json() == {"user_id": "user-alice", "tier": "pro"}
