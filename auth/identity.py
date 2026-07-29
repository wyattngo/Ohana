"""Verified identity (R1.1 extended for multi-tenant).

`Identity(user_id, shop_id, role)` is the sole source of tenant scope. `shop_id` MUST
come from a signed JWT claim — never from a request body, header, or webhook payload
(the platform never trusts client-supplied tenancy). `verify_token` rejects tokens that
omit `shop_id` outright: there is no default fall-through, because "missing shop_id" is
indistinguishable from "attacker forged a token for a shop they don't own" if we allow one.

Algorithm is pinned to a literal list (HS256 for GĐ0 shared-secret dev flow; RS256 is a
Phase 3+ upgrade). Reading it from config would open the classic alg-confusion bypass.

`identity_from_cookie` (spec 04 P0) is the browser-flow counterpart to `verify_token`: it
sources the SAME signed token from the httpOnly `ohana_session` cookie instead of an
`Authorization` header, so a FastAPI route can depend on either transport while sharing one
verification path. No new trust surface is added — a forged/missing/expired cookie hits the
identical `verify_token` rejection as a forged header token, just re-shaped as a 401 HTTP
response instead of a raised exception.

`get_jwt_secret()` reads `OHANA_JWT_SECRET` via `app.config.Settings` (spec 05 Phase P2 —
migrated off the direct `os.environ.get(...)` it used before; `db/session.py` got the same
treatment). Its literal dev fallback is gated on `OHANA_ENV == "dev"` and raises anywhere
else; the reasoning for that gate (mint fail-closed + verify fail-open is not a safe pair) is
on the function itself.

P2 deliberately builds a FRESH `Settings()` per call instead of going through the
`@lru_cache`d `app.config.get_settings()`. `app/main.py` calls `default_embedder()` at
IMPORT time (spec 05 P1), which calls `get_settings()` and caches whatever env happened to
be present at first import of that module — in a test process, that can be before any test's
`monkeypatch.setenv/delenv` runs. Routing this security gate through that same cache would
mean `test_jwt_secret_refuses_public_fallback_outside_dev`'s `monkeypatch.delenv(...)` could
silently no-op against an already-cached instance, which is exactly the "gate looks green but
isn't actually exercising the fail-closed path" failure ISSUE-016 already burned this repo on
once (there, a mocked embedder; here, it would be a stale cache). A fresh `Settings()` has no
shared state to go stale — it reads `os.environ` at construction, same as the pre-P2 direct
read did on every call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from db.shop_repo import ShopRepo

_ALLOWED_ALGOS = ["HS256"]

SESSION_COOKIE_NAME = "ohana_session"
CSRF_COOKIE_NAME = "ohana_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"


@dataclass(frozen=True)
class Identity:
    user_id: str
    shop_id: str
    role: str


def verify_token(token: str, *, secret: str) -> Identity:
    """Verify an HS256 JWT and project it to Identity.

    Raises:
        jwt.InvalidSignatureError: signature mismatch (bad secret / tampered token).
        jwt.InvalidTokenError (or subclass): any other JWT decode failure.
        ValueError: token decoded but the `shop_id` claim is missing/empty. Also raised
            if `sub` or `role` are missing — no silent defaults for identity fields.
    """
    claims = jwt.decode(token, secret, algorithms=_ALLOWED_ALGOS)

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise ValueError("invalid_sub — token missing 'sub' claim")

    shop_id = claims.get("shop_id")
    if not isinstance(shop_id, str) or not shop_id:
        raise ValueError("invalid_shop_id — token missing 'shop_id' claim")

    role = claims.get("role")
    if not isinstance(role, str) or not role:
        raise ValueError("invalid_role — token missing 'role' claim")

    return Identity(user_id=sub, shop_id=shop_id, role=role)


def get_jwt_secret() -> str:
    """`OHANA_JWT_SECRET` via a fresh `Settings()` (see module docstring for why fresh, not
    the cached `get_settings()`). The literal dev fallback applies ONLY when
    `OHANA_ENV == "dev"`; anywhere else a missing secret raises rather than silently signing
    with a value that is public in git.

    The fallback cannot be scoped by the mock-authorize guard alone: that guard closes the
    route that MINTS tokens, but this secret also feeds the path that VERIFIES them
    (`identity_from_cookie`). A deploy that forgot `OHANA_JWT_SECRET` would 404 the mint route
    yet still accept any cookie forged with the public fallback — self-issued `shop_id`, full
    cross-tenant read (R1.22). Mint fail-closed + verify fail-open is not a safe pair, so the
    fallback is gated on the same dev signal as the mint route.
    """
    settings = Settings()
    if settings.ohana_jwt_secret:
        return settings.ohana_jwt_secret
    if settings.ohana_env != "dev":
        raise RuntimeError(
            "OHANA_JWT_SECRET is required outside dev — refusing the public dev fallback."
        )
    return "ohana-dev-insecure-secret-change-before-staging"


def identity_from_cookie(request: Request) -> Identity:
    """FastAPI dependency — derive `Identity` from the httpOnly `ohana_session` cookie
    (browser flow). Missing or invalid cookie -> 401, same shape either way (never reveal
    which — a present-but-invalid cookie and a missing one are indistinguishable to the
    caller, mirroring the no-default-fallthrough invariant `verify_token` already enforces).
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="missing_session_cookie")
    try:
        return verify_token(token, secret=get_jwt_secret())
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="invalid_session_cookie") from exc


def build_active_shop_dep(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[..., Awaitable[Identity]]:
    """Dependency factory (spec 11 S1) — `identity_from_cookie` + đối chiếu `shops`.

    **Vì sao lớp này tồn tại.** `verify_token` chỉ chứng minh token được ký bằng secret của
    ta và có đủ claim. Nó KHÔNG chứng minh `shop_id` trong claim trỏ tới một shop có thật:
    trước S1, một token ký đúng mang `shop_id` là chuỗi bất kỳ đi thẳng vào mọi tầng dưới,
    và bảng `shops` (S0) không ai hỏi tới. Ca cụ thể nhất không phải kẻ tấn công mà là token
    dev fixture (`fixture-shop-001`) lọt sang môi trường khác.

    **Vì sao KHÔNG nhét DB vào `verify_token`.** Verify chữ ký là hàm thuần, đồng bộ, test
    được không cần Postgres — kéo I/O vào đó làm mọi test chữ ký phải dựng DB, và trộn hai
    câu hỏi khác nhau ("token có thật không" vs "shop còn hoạt động không") vào một chỗ.
    Tách ra thì mỗi tầng hỏng theo cách riêng và đọc log biết ngay tầng nào.

    **Chưa cache** (Wyatt ký 2026-07-20): mỗi request thêm một PK lookup. Cache sai key là
    cross-tenant leak KHÔNG đi qua SQL nên FK không cứu được (R2, spec §4). Chưa có shop
    thật để đo tải ⇒ đo trước, cache sau — và khi thêm thì phải kèm test 2 shop song song.
    """

    async def _dep(identity: Identity = Depends(identity_from_cookie)) -> Identity:
        async with session_factory() as session:
            shop = await ShopRepo(session).get_active(identity.shop_id)
        if shop is None:
            # 401 (không phải 403) và CÙNG một detail cho cả "không tồn tại" lẫn "bị treo":
            # phân biệt hai ca đó cho kẻ tấn công biết `shop_id` nào là thật. Cùng hình dạng
            # với `identity_from_cookie` — cookie thiếu và cookie hỏng cũng không phân biệt.
            raise HTTPException(status_code=401, detail="invalid_session_cookie")
        return identity

    return _dep


def build_admin_dep(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[..., Awaitable[Identity]]:
    """`build_active_shop_dep` + role check — bản có kiểm shop của `require_admin`.

    Phải bọc lên dependency MỚI chứ không phải `identity_from_cookie`: nếu quên, đường admin
    thành cửa DUY NHẤT không kiểm shop, và nó lại đúng là cửa quyền cao nhất.
    """
    active_shop_dep = build_active_shop_dep(session_factory)

    async def _dep(identity: Identity = Depends(active_shop_dep)) -> Identity:
        if identity.role != "admin":
            raise HTTPException(status_code=403, detail="admin_role_required")
        return identity

    return _dep


def require_admin(identity: Identity = Depends(identity_from_cookie)) -> Identity:
    """FastAPI dependency (spec 04 P0) — layers a role check on top of `identity_from_cookie`.
    A missing/invalid session cookie still 401s (from the wrapped dependency, resolved first);
    an authenticated identity whose `role != "admin"` gets 403 here.

    Spec 04 §7 Phase P2 step 1 literally says "GET /api/admin/wiki/ingest với non-admin cookie
    → 403", but the route this guards (`api/admin.py`) is POST-only — a GET there 405s before
    ever reaching this dependency. The correct read is "POST with a non-admin cookie → 403",
    which is what this function (and `tests/test_admin_ui.py`) actually implements.

    403, not 404: this deliberately differs from `mock_authorize`'s and `api/inbox.py`'s "look
    like it doesn't exist" shape, which is aimed at callers with NO valid session at all. An
    authenticated seller hitting this route already knows they're logged in as a seller —
    confirming the admin route exists leaks nothing beyond what they could already infer.
    """
    if identity.role != "admin":
        raise HTTPException(status_code=403, detail="admin_role_required")
    return identity
