"""Dev-only mock authorize endpoint (spec 04 DEC-OHANA-01 U4).

Mints a fixture JWT and sets it as the `ohana_session` httpOnly cookie so the browser SPA
can call the JWT-gated `/api/*` routes without a real login flow. GD0.5 has no seller
accounts yet (PRE-004 unresolved) — this is the ONLY way to reach `/api/inbox` from a
browser until spec 05 lands real login.

Hard-guarded behind `OHANA_ENV == "dev"`, checked PER-REQUEST (not baked in at import/build
time) and FAIL-CLOSED: an unset or misconfigured `OHANA_ENV` denies (404) rather than
defaults to allow. This is a deliberate reading of the literal DEC-OHANA-01 U4 wording
("hardcode check, không lấy từ env var (bypass risk)") — flipped toward the direction that
actually closes the bypass: the risk isn't "an env var exists", it's "a deploy silently
defaults to exposed". No `app.config`/`Settings` object exists yet in this codebase (Phase
3+ per `db/session.py`'s established convention) to hang a non-env-var `settings.env` off
of, so a literal single-constant hardcode would mean this route can never be enabled in ANY
environment without a source edit — impractical for local dev. Flagged for Wyatt in the P0
ANCHOR report; happy to swap to a literal `if False:`-style hardcode if that reading was
intended.

The 404 (not 403) shape matches spec 01's admin-ingest precedent — outside dev this route
should look, to an unauthenticated prober, like it doesn't exist at all.

Also mints a non-httpOnly `ohana_csrf` cookie (double-submit CSRF token, enforced by the
CSRF middleware in `app/main.py`) — P1's `web/src/lib/api.ts` reads it and echoes it back as
the `X-CSRF-Token` header on state-mutating requests (approve/reject).
"""

from __future__ import annotations

import os
import secrets

import jwt
from fastapi import APIRouter, HTTPException, Response

from auth.identity import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, get_jwt_secret

_FIXTURE_USER_ID = "dev-user-001"
_FIXTURE_OA_ID = "fixture-oa-001"
_FIXTURE_SHOP_ID = "fixture-shop-001"
_SESSION_MAX_AGE_SECONDS = 24 * 60 * 60  # 24h dev convenience — no refresh flow at GD0.5
_ALLOWED_ROLES = ("seller", "admin")


def _is_dev_env() -> bool:
    """Checked per-request (not baked in at router-build time) and FAIL-CLOSED: unset or any
    value other than the literal "dev" denies. A deploy that forgets to set OHANA_ENV must
    NOT accidentally expose this route — see module docstring for the DEC-OHANA-01 reading."""
    return os.environ.get("OHANA_ENV") == "dev"


async def _ensure_fixture_shop() -> None:
    """Tạo row `shops` cho fixture shop nếu chưa có. CHỈ gọi từ nhánh đã qua `_is_dev_env()`.

    Dùng session riêng thay vì nhận `session_factory` qua `build_router`: giữ chữ ký
    `build_router()` không đổi (nó được gọi ở `app/main.py` và trong test), và đây là đường
    dev-only nên không đáng đánh đổi API của router production.
    """
    from sqlalchemy import text

    from db.session import make_session_factory

    session_factory = make_session_factory()
    async with session_factory() as session:
        await session.execute(
            text(
                "insert into shops (id, name, status) values (:i, :n, 'active') "
                "on conflict (id) do nothing"
            ),
            {"i": _FIXTURE_SHOP_ID, "n": "Dev Fixture Shop"},
        )
        await session.commit()


def build_router() -> APIRouter:
    router = APIRouter(prefix="/mock", tags=["mock-auth-dev-only"])

    @router.post("/authorize")
    async def mock_authorize(response: Response, role: str = "seller") -> dict[str, str]:
        if not _is_dev_env():
            raise HTTPException(status_code=404)
        if role not in _ALLOWED_ROLES:
            raise HTTPException(status_code=422, detail="invalid_role")

        # Spec 11 S1 — seed fixture shop vào `shops`, CHỈ ở dev.
        #
        # Từ S1, mọi route đối chiếu `shop_id` với bảng `shops`. Không seed thì luồng dev vỡ
        # theo kiểu tệ nhất: `mock/authorize` vẫn trả 200 (nó chỉ mint token), rồi MỌI route
        # sau đó 401 vì `fixture-shop-001` không có trong bảng. Tầng mint nói OK, tầng dùng
        # nói không — người debug sẽ đi soi JWT chứ không nghĩ tới bảng shops.
        #
        # Idempotent (`on conflict do nothing`) và nằm SAU `_is_dev_env()`: ngoài dev route
        # này đã 404 từ dòng trên, nên không có đường nào seed vào production.
        await _ensure_fixture_shop()

        token = jwt.encode(
            {"sub": _FIXTURE_USER_ID, "shop_id": _FIXTURE_SHOP_ID, "role": role},
            get_jwt_secret(),
            algorithm="HS256",
        )
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            httponly=True,
            secure=False,  # localhost only per DEC-OHANA-01 U3 — flip when staging lands
            samesite="lax",
            max_age=_SESSION_MAX_AGE_SECONDS,
        )
        response.set_cookie(
            key=CSRF_COOKIE_NAME,
            value=secrets.token_urlsafe(32),
            httponly=False,  # JS must read this to echo back as X-CSRF-Token
            secure=False,
            samesite="lax",
            max_age=_SESSION_MAX_AGE_SECONDS,
        )
        return {"oa_id": _FIXTURE_OA_ID, "shop_id": _FIXTURE_SHOP_ID, "role": role}

    return router
