"""Outbound Zalo OA sender — STUB (PRE-004 unresolved).

Spec 01 §6 fallback: "STOP F3 send-leg — build draft engine + inbox UI với mock sender
trước." This module ships a `ZaloSender` Protocol and a `MockZaloSender` that records
sends without any network. The real HTTP sender lands when PRE-004 clears:
  - Zalo OA access token (per-shop) + refresh flow
  - Webhook signature verification (inbound)
  - 8-msg / 48-hour reactive-window enforcement (rate-limit warning surface)

Anything that DOES land here later must keep `verify=True` hardcoded (R1.3) and NEVER
accept `shop_id` from a request body — the sender must be constructed against a shop
context that came from `auth.identity.Identity`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bridge.zalo_token_refresh import refresh_shop_tokens
from db.repos import ZaloOATokenRepo

logger = logging.getLogger(__name__)

_MESSAGE_URL = "https://openapi.zalo.me/v3.0/oa/message/cs"

# Error code → SendPolicyError category (docs Zalo). Chỉ những code có action downstream
# khác nhau mới map — code khác raise ZaloSendError generic (không nuốt im lặng).
_POLICY_ERRORS = {
    -201: "invalid_recipient",  # user chưa follow / block OA
    -224: "out_of_window",  # ngoài reactive window 48h (sau 1/1/2026 không còn cap 8-msg)
    -114: "rate_limited",  # rate limit
}
_TOKEN_EXPIRED = -32


class ZaloSender(Protocol):
    async def send(self, *, shop_id: str, customer_id: str, text: str) -> None: ...


class ZaloSendError(Exception):
    """Gửi thất bại không phân loại được — Zalo trả error code lạ, hoặc shop chưa có token."""


class SendPolicyError(ZaloSendError):
    """Zalo từ chối vì CHÍNH SÁCH (không phải lỗi hạ tầng) — invalid_recipient / out_of_window
    / rate_limited. Mang `.category` để orchestrator/policy_gate phân nhánh action:
    invalid_recipient → mark customer unreachable; out_of_window → escalate seller manual;
    rate_limited → backoff enqueue. KHÔNG gộp 1 exception vì mỗi loại xử lý khác nhau."""

    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(f"{category}: {message}")


@dataclass
class MockZaloSender:
    """GĐ0 default. Records every send in `.sends` and logs at INFO. Zero network I/O.

    Swap for a live `ZaloAPISender` once PRE-004 clears — the interface stays the same
    so orchestrator wiring doesn't change (R6 pair).
    """

    sends: list[dict[str, Any]] = field(default_factory=list)

    async def send(self, *, shop_id: str, customer_id: str, text: str) -> None:
        self.sends.append({"shop_id": shop_id, "customer_id": customer_id, "text": text})
        logger.info(
            "zalo_mock_send shop_id=%s customer_id=%s text_len=%d",
            shop_id,
            customer_id,
            len(text),
        )


class HttpZaloSender:
    """Real Zalo OA sender (spec 17 P2). Implement `ZaloSender` Protocol.

    Gửi `POST openapi.zalo.me/v3.0/oa/message/cs` với header `access_token` (KHÔNG Bearer —
    bẫy Zalo). Access token lookup per-shop từ `zalo_oa_tokens` mỗi send (không cache trong
    sender vì refresh cron có thể đổi token bất cứ lúc nào — cache = gửi bằng token cũ đã
    chết). error `-32` (expired) → refresh 1 lần + retry (refresh single-use nên KHÔNG loop).

    `client` inject cho test (MockTransport); production build lazy với `verify=True` hardcode
    (R1.3 — không path nào tắt TLS verify được). `app_id`/`app_secret` per-App cho refresh.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        app_id: str,
        app_secret: str,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._sf = session_factory
        self._app_id = app_id
        self._app_secret = app_secret

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        # verify=True hardcode (R1.3) — không config nào tắt được. timeout 5s: Zalo p95 ~1s,
        # vượt = Zalo sự cố, không phải bug ta.
        self._client = httpx.AsyncClient(verify=True, timeout=5.0)
        return self._client

    async def _access_token(self, shop_id: str) -> str:
        async with self._sf() as session:
            row = await ZaloOATokenRepo(session).get_by_shop(shop_id)
        if row is None:
            raise ZaloSendError(f"no zalo token for shop {shop_id!r}")
        return row.access_token

    async def _post_message(self, access_token: str, customer_id: str, text: str) -> int:
        """Gửi 1 message, trả `error` code từ response envelope."""
        client = self._ensure_client()
        resp = await client.post(
            _MESSAGE_URL,
            headers={"access_token": access_token},
            json={"recipient": {"user_id": customer_id}, "message": {"text": text}},
        )
        # Zalo trả 200 kèm envelope {error, message, data} kể cả khi error != 0.
        try:
            data = resp.json()
            return int(data["error"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ZaloSendError(f"zalo send malformed response (http {resp.status_code})") from exc

    def _raise_for_error(self, error: int) -> None:
        """Map error code → exception. error==0 là success (caller không gọi hàm này)."""
        category = _POLICY_ERRORS.get(error)
        if category is not None:
            raise SendPolicyError(category, f"zalo error {error}")
        raise ZaloSendError(f"zalo send unexpected error code {error}")

    async def send(self, *, shop_id: str, customer_id: str, text: str) -> None:
        access = await self._access_token(shop_id)
        error = await self._post_message(access, customer_id, text)
        if error == 0:
            return

        if error == _TOKEN_EXPIRED:
            # Refresh single-use → retry ĐÚNG 1 lần (loop = burn hết refresh_token). Refresh
            # fail ⇒ ZaloRefreshError bubble (không nuốt). Retry vẫn lỗi ⇒ map error mới.
            await refresh_shop_tokens(
                shop_id,
                self._sf,
                client=self._ensure_client(),
                app_id=self._app_id,
                app_secret=self._app_secret,
                force=True,  # Zalo báo -32 = token chết bất kể DB expiry ⇒ bỏ double-check
            )
            access = await self._access_token(shop_id)
            error = await self._post_message(access, customer_id, text)
            if error == 0:
                return

        self._raise_for_error(error)

    async def aclose(self) -> None:
        """Đóng client nếu sender tự dựng (không đóng client inject từ test/caller)."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
