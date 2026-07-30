"""Spec 17 P2 — Zalo OAuth v4 token refresh (RISK: high, RED first).

Refresh_token Zalo là SINGLE-USE: mỗi call `oauth.zaloapp.com/v4/oa/access_token` sinh cặp
(access, refresh) MỚI, cặp cũ chết ngay. Hai process refresh cùng shop mà không lock =
process 1 ghi cặp mới, process 2 refresh trên refresh_token đã CHẾT → 401 → mất luôn khả
năng refresh (phải re-authorize manual qua OA admin). `SELECT ... FOR UPDATE` từ P0 repo +
double-check (`access_expires_at > now() + margin` sau khi lấy lock) chặn điều đó.

Test dùng `httpx.MockTransport` inject AsyncClient — KHÔNG hit Zalo thật, KHÔNG thêm dep
`respx` (project pin strict). Cùng pattern `bridge/ohana_client.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from bridge.zalo_token_refresh import ZaloRefreshError, refresh_shop_tokens
from db.models import Shop
from db.repos import ZaloOATokenRepo

_APP_ID = "app-123"
_APP_SECRET = "app-secret-xyz"
_OA_ID = "oa-999"


async def _seed_token(
    session_factory,
    *,
    access: str = "old-access",
    refresh: str = "old-refresh",
    access_ttl_s: int = -60,  # default EXPIRED (âm = quá khứ) để refresh chạy
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as s:
        s.add(Shop(id="shop_a", name="Shop A"))
        await s.commit()
        repo = ZaloOATokenRepo(s)
        await repo.update_tokens_locked(
            shop_id="shop_a",
            oa_id=_OA_ID,
            access_token=access,
            refresh_token=refresh,
            access_expires_at=now + timedelta(seconds=access_ttl_s),
            refresh_expires_at=now + timedelta(days=90),
            oa_secret_key="s",
        )


def _oauth_handler(response_json: dict[str, Any], status: int = 200):
    """MockTransport handler trả response JSON cố định + ghi lại request để assert."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["content"] = request.content.decode("utf-8")
        return httpx.Response(status, json=response_json)

    return handler, captured


@pytest.mark.asyncio
async def test_refresh_writes_new_token_pair(fresh_db):
    """Happy path: POST oauth → response cặp mới → ghi DB, cặp cũ bị thay."""
    _, session_factory = await fresh_db()
    await _seed_token(session_factory)

    handler, captured = _oauth_handler(
        {
            "access_token": "NEW-access",
            "refresh_token": "NEW-refresh",
            "expires_in": "3600",
        }
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await refresh_shop_tokens(
        "shop_a",
        session_factory,
        client=client,
        app_id=_APP_ID,
        app_secret=_APP_SECRET,
    )

    # Request đúng shape: header secret_key, body grant_type=refresh_token
    assert "oauth.zaloapp.com/v4/oa/access_token" in captured["url"]
    assert captured["headers"].get("secret_key") == _APP_SECRET
    assert "grant_type=refresh_token" in captured["content"]
    assert "old-refresh" in captured["content"]

    # DB có cặp mới
    async with session_factory() as s:
        row = await ZaloOATokenRepo(s).get_by_shop("shop_a")
    assert row is not None
    assert row.access_token == "NEW-access"
    assert row.refresh_token == "NEW-refresh"


@pytest.mark.asyncio
async def test_refresh_double_check_skips_when_already_fresh(fresh_db):
    """Nếu token đã fresh (access_expires_at còn xa) khi lấy lock ⇒ KHÔNG refresh.

    Đây là double-check locking: process 2 chờ lock của process 1, lấy được lock thì thấy
    token đã fresh (process 1 vừa refresh) ⇒ dùng luôn, KHÔNG đốt refresh_token lần nữa.
    """
    _, session_factory = await fresh_db()
    # Token access còn 1 giờ = fresh
    await _seed_token(session_factory, access_ttl_s=3600)

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            200,
            json={
                "access_token": "SHOULD-NOT",
                "refresh_token": "SHOULD-NOT",
                "expires_in": "3600",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await refresh_shop_tokens(
        "shop_a",
        session_factory,
        client=client,
        app_id=_APP_ID,
        app_secret=_APP_SECRET,
    )

    assert call_count["n"] == 0, "token fresh ⇒ KHÔNG được gọi oauth (double-check locking)"
    async with session_factory() as s:
        row = await ZaloOATokenRepo(s).get_by_shop("shop_a")
    assert row.access_token == "old-access", "token fresh không bị thay"


@pytest.mark.asyncio
async def test_refresh_401_raises_ZaloRefreshError(fresh_db):
    """Zalo trả 401 (refresh_token đã chết/invalid) ⇒ raise, KHÔNG ghi rác vào DB."""
    _, session_factory = await fresh_db()
    await _seed_token(session_factory)

    handler, _ = _oauth_handler({"error": -14002, "message": "refresh token invalid"}, status=401)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(ZaloRefreshError):
        await refresh_shop_tokens(
            "shop_a",
            session_factory,
            client=client,
            app_id=_APP_ID,
            app_secret=_APP_SECRET,
        )

    # DB giữ nguyên cặp cũ (không ghi đè bằng lỗi)
    async with session_factory() as s:
        row = await ZaloOATokenRepo(s).get_by_shop("shop_a")
    assert row.refresh_token == "old-refresh"


@pytest.mark.asyncio
async def test_refresh_malformed_response_raises(fresh_db):
    """Response 200 nhưng thiếu access_token/refresh_token ⇒ raise, không ghi cặp thiếu."""
    _, session_factory = await fresh_db()
    await _seed_token(session_factory)

    handler, _ = _oauth_handler({"error": 0})  # thiếu access_token, refresh_token
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(ZaloRefreshError):
        await refresh_shop_tokens(
            "shop_a",
            session_factory,
            client=client,
            app_id=_APP_ID,
            app_secret=_APP_SECRET,
        )


@pytest.mark.asyncio
async def test_refresh_unknown_shop_raises(fresh_db):
    """Shop chưa có token row ⇒ raise (không có refresh_token để dùng)."""
    _, session_factory = await fresh_db()
    # KHÔNG seed token

    handler, _ = _oauth_handler({"access_token": "x", "refresh_token": "y", "expires_in": "3600"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(ZaloRefreshError):
        await refresh_shop_tokens(
            "shop-nonexistent",
            session_factory,
            client=client,
            app_id=_APP_ID,
            app_secret=_APP_SECRET,
        )
