"""Spec 17 P2 — HttpZaloSender real HTTP send (RISK: high, RED first).

Gửi `POST openapi.zalo.me/v3.0/oa/message/cs` với header `access_token` (KHÔNG Bearer),
body `{recipient:{user_id}, message:{text}}`. Response envelope `{error, message, data}`:
- `error: 0` → success
- `error: -32` → token expired → refresh 1 lần + retry (refresh single-use nên KHÔNG loop)
- `error: -201/-224/-114` → raise `SendPolicyError(category)` để orchestrator/policy phân nhánh

Test dùng `httpx.MockTransport` (không hit Zalo, không thêm dep respx).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from bridge.zalo_sender import HttpZaloSender, SendPolicyError, ZaloSendError
from bridge.zalo_token_refresh import ZaloRefreshError
from db.models import Shop
from db.repos import ZaloOATokenRepo

_APP_ID = "app-123"
_APP_SECRET = "app-secret"
_OA_ID = "oa-1"
_USER_ID = "user-42"


async def _seed(session_factory, *, access: str = "tok-good") -> None:
    now = datetime.now(UTC)
    async with session_factory() as s:
        s.add(Shop(id="shop_a", name="Shop A"))
        await s.commit()
        await ZaloOATokenRepo(s).update_tokens_locked(
            shop_id="shop_a",
            oa_id=_OA_ID,
            access_token=access,
            refresh_token="refresh-1",
            access_expires_at=now + timedelta(hours=1),
            refresh_expires_at=now + timedelta(days=90),
            oa_secret_key="s",
        )


def _sender(session_factory, handler) -> HttpZaloSender:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpZaloSender(
        client,
        session_factory,
        app_id=_APP_ID,
        app_secret=_APP_SECRET,
    )


@pytest.mark.asyncio
async def test_send_success(fresh_db):
    """error:0 → gửi thành công, request shape đúng (header access_token + body)."""
    _, session_factory = await fresh_db()
    await _seed(session_factory)

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["content"] = request.content.decode("utf-8")
        return httpx.Response(
            200, json={"error": 0, "message": "Success", "data": {"message_id": "m1"}}
        )

    sender = _sender(session_factory, handler)
    await sender.send(shop_id="shop_a", customer_id=_USER_ID, text="chào anh")

    assert "openapi.zalo.me/v3.0/oa/message/cs" in captured["url"]
    assert captured["headers"].get("access_token") == "tok-good"
    assert "authorization" not in captured["headers"], "KHÔNG dùng Bearer — bẫy Zalo"
    assert _USER_ID in captured["content"]
    assert "chào anh" in captured["content"]


@pytest.mark.asyncio
async def test_send_token_expired_refreshes_and_retries(fresh_db):
    """error:-32 lần đầu → refresh → retry với token mới → thành công."""
    _, session_factory = await fresh_db()
    await _seed(session_factory, access="tok-expired")

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "oauth.zaloapp.com" in url:
            calls.append("refresh")
            return httpx.Response(
                200,
                json={
                    "access_token": "tok-fresh",
                    "refresh_token": "refresh-2",
                    "expires_in": "3600",
                },
            )
        # message/cs
        access = request.headers.get("access_token")
        calls.append(f"send:{access}")
        if access == "tok-expired":
            return httpx.Response(
                200, json={"error": -32, "message": "access token expired", "data": None}
            )
        return httpx.Response(200, json={"error": 0, "message": "Success", "data": {}})

    sender = _sender(session_factory, handler)
    await sender.send(shop_id="shop_a", customer_id=_USER_ID, text="hi")

    assert calls == ["send:tok-expired", "refresh", "send:tok-fresh"], (
        "phải: gửi lỗi -32 → refresh → gửi lại token mới thành công"
    )


@pytest.mark.asyncio
async def test_send_token_expired_refresh_fails_raises(fresh_db):
    """error:-32 → refresh fail (401) → raise (không retry loop vô hạn)."""
    _, session_factory = await fresh_db()
    await _seed(session_factory, access="tok-expired")

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth.zaloapp.com" in str(request.url):
            return httpx.Response(401, json={"error": -14002, "message": "invalid"})
        return httpx.Response(200, json={"error": -32, "message": "expired", "data": None})

    sender = _sender(session_factory, handler)
    with pytest.raises((ZaloRefreshError, ZaloSendError)):
        await sender.send(shop_id="shop_a", customer_id=_USER_ID, text="hi")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code,category",
    [
        (-201, "invalid_recipient"),
        (-224, "out_of_window"),
        (-114, "rate_limited"),
    ],
)
async def test_send_policy_errors_raise_categorized(fresh_db, error_code, category):
    """error -201/-224/-114 → SendPolicyError với category đúng để downstream phân nhánh."""
    _, session_factory = await fresh_db()
    await _seed(session_factory)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": error_code, "message": "policy", "data": None})

    sender = _sender(session_factory, handler)
    with pytest.raises(SendPolicyError) as exc:
        await sender.send(shop_id="shop_a", customer_id=_USER_ID, text="hi")
    assert exc.value.category == category


@pytest.mark.asyncio
async def test_send_unknown_shop_raises(fresh_db):
    """Shop chưa có token ⇒ raise (không thể gửi mà không có access_token)."""
    _, session_factory = await fresh_db()
    # KHÔNG seed

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": 0, "message": "Success", "data": {}})

    sender = _sender(session_factory, handler)
    with pytest.raises(ZaloSendError):
        await sender.send(shop_id="shop-nonexistent", customer_id=_USER_ID, text="hi")


@pytest.mark.asyncio
async def test_send_unexpected_error_code_raises(fresh_db):
    """error code lạ (không phải 0/-32/-201/-224/-114) ⇒ raise, không nuốt im lặng."""
    _, session_factory = await fresh_db()
    await _seed(session_factory)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": -99999, "message": "weird", "data": None})

    sender = _sender(session_factory, handler)
    with pytest.raises(ZaloSendError):
        await sender.send(shop_id="shop_a", customer_id=_USER_ID, text="hi")
