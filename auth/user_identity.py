"""User identity cho Tầng 2 (D7 · ADR ratified 2026-08-01 · Phase 2.4a).

TÁCH khỏi `auth/identity.py::Identity` (Tầng 3, có `shop_id`). Tầng 2 per-user,
không có shop concept — `UserIdentity(user_id, tier)` là hai field, mypy giữ tường
lửa: wire nhầm route Tầng 3 với `UserIdentity` ⇒ mypy đỏ.

**Same secret + allowed algo với `verify_token`** (`_ALLOWED_ALGOS = ["HS256"]` pinned
literal, đọc `OHANA_JWT_SECRET` qua `get_jwt_secret`). **Khác payload validator:**
- `role` PHẢI == "user" (KHÔNG seller/admin nào lọt vào route Tầng 2).
- `sub` (user_id) required.
- `tier` required, PHẢI ∈ {"free", "pro"} — whitelist ĐÓNG, thêm tier mới phải sửa
  cả `_ALLOWED_TIERS` và `TIER_LIMITS` (agent/assistant_tier.py) cùng lúc.
- KHÔNG kiểm `shop_id` (Tầng 2 không có).

**Same cookie `ohana_session`** với seller flow (chia sẻ transport cho browser SPA).
Route Tầng 2 chỉ chấp nhận token role `user`; seller/admin token trong cookie ⇒ 401
cùng shape với missing/invalid (no leak về ai đang login).
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request

from auth.identity import SESSION_COOKIE_NAME, get_jwt_secret

# Pin literal — KHÔNG đọc từ config. Đọc config = alg-confusion bypass (attacker gửi
# `alg: none` hoặc RS256 với secret HS256). Cùng bài `auth/identity.py::_ALLOWED_ALGOS`.
_ALLOWED_ALGOS = ["HS256"]

# Whitelist ĐÓNG — value ngoài đây ⇒ raise. Thêm tier mới ("enterprise", "team"):
# sửa cả set này và `TIER_LIMITS` trong `agent/assistant_tier.py` (2 chỗ, cố ý —
# một chỗ đổi chỉ tạo silent hole nếu chỗ kia không đổi theo).
_ALLOWED_TIERS = frozenset({"free", "pro"})

# Role literal — Tầng 2 chỉ 1 role. Seller/admin token có role khác, verify_user_token
# reject.
_USER_ROLE = "user"


@dataclass(frozen=True)
class UserIdentity:
    """Verified user identity cho Tầng 2. `user_id` (JWT `sub`) là nguồn scope DUY NHẤT
    — không bao giờ đọc từ body/header/query. `tier` từ claim, whitelist enforce."""

    user_id: str
    tier: str  # "free" | "pro"


def verify_user_token(token: str, *, secret: str) -> UserIdentity:
    """Verify HS256 JWT và project ra `UserIdentity`.

    Raises:
        jwt.InvalidSignatureError: signature mismatch (bad secret / tampered token).
        jwt.InvalidTokenError (or subclass): JWT decode failure khác (alg confusion,
            expired, malformed).
        ValueError: token decode OK nhưng shape sai — `sub`/`role`/`tier` thiếu hoặc
            `role != "user"` hoặc `tier` ngoài whitelist. Không silent default: attacker
            forge token thiếu claim = attacker forge success nếu ta im lặng.
    """
    claims = jwt.decode(token, secret, algorithms=_ALLOWED_ALGOS)

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise ValueError("invalid_sub — token missing 'sub' claim")

    role = claims.get("role")
    if role != _USER_ROLE:
        raise ValueError(f"invalid_role — expected 'user', got {role!r}")

    tier = claims.get("tier")
    if not isinstance(tier, str) or tier not in _ALLOWED_TIERS:
        raise ValueError(f"invalid_tier — must be 'free' or 'pro', got {tier!r}")

    return UserIdentity(user_id=sub, tier=tier)


def user_identity_from_cookie(request: Request) -> UserIdentity:
    """FastAPI dependency — derive `UserIdentity` từ cookie `ohana_session`.

    Missing hoặc invalid cookie ⇒ 401 cùng detail (`invalid_session_cookie`). Không
    phân biệt "cookie thiếu" vs "cookie hỏng" vs "role sai" cho caller — mirror
    `auth/identity.py::identity_from_cookie` no-default-fallthrough invariant, và không
    leak "route Tầng 2 tồn tại nhưng bạn đang login sai role" cho seller/admin token.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="missing_session_cookie")
    try:
        return verify_user_token(token, secret=get_jwt_secret())
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="invalid_session_cookie") from exc
