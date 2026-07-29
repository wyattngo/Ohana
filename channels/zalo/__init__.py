"""Zalo channel adapter (spec 06 Phase F1).

Wraps the EXISTING `bridge/zalo_sender.py` rather than replacing it: `ZaloSender`'s
signature is a contract Spec 03c depends on when it swaps `MockZaloSender` for the real
HTTP sender. This adapter adds the channel seam without touching that file.

The inbound payload shape below is the GĐ0 placeholder — the same `{customer_id, message}`
the pre-F1 webhook accepted. PRE-004 brings Zalo's real webhook envelope; when it lands,
only `parse_inbound` changes, because the core no longer knows what a Zalo payload is.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bridge.zalo_sender import ZaloSender
from channels.base import InboundMessage
from channels.zalo.signature import verify_zalo_signature
from db.repos import ZaloOATokenRepo

logger = logging.getLogger(__name__)


class ZaloChannel:
    name = "zalo"

    def __init__(self, *, sender: ZaloSender) -> None:
        self._sender = sender

    async def verify_signature(
        self, req: Request, session_factory: async_sessionmaker[AsyncSession]
    ) -> bytes:
        """Verify webhook signature TRƯỚC parse — trả raw body verified hoặc raise HTTPException.

        Đây là seam channel-scoped: `api/webhook.py` KHÔNG biết Zalo hay Messenger dùng scheme
        gì; nó gọi `adapter.verify_signature(req, session_factory)`. Adapter THIẾU method này
        ⇒ core raise 501 (spec 17 P1 HIGH 1: fail-loud, KHÔNG silent-skip) — nên production
        adapter BẮT BUỘC implement, test adapter cố tình no-op để bypass tường minh.

        Session_factory được tiêm thay vì repo instance vì lookup key là 1 short-lived query,
        không muốn giữ session mở qua toàn request lifecycle. Repo tự đóng session khi ra khỏi
        `async with`.
        """
        async with session_factory() as session:
            repo = ZaloOATokenRepo(session)
            return await verify_zalo_signature(req, repo.get_oa_secret_by_oa_id)

    def parse_inbound(self, payload: Mapping[str, Any]) -> InboundMessage | None:
        """Envelope Zalo THẬT (docs 2026-07-24): `{app_id, sender:{id}, recipient:{id},
        event_name, message:{text, msg_id, ...}, timestamp}`. Chỉ `user_send_text` được xử lý;
        event khác trả `None` (skip, log INFO — crash = webhook 500 = Zalo retry storm 5 lần).
        """
        event_name = payload.get("event_name")
        if event_name != "user_send_text":
            # Skip có chủ đích: image/sticker/gif/file/location/oa_send_*/anonymous/read-receipt/
            # event Zalo thêm mới. Spec 17 chỉ text — các loại khác land ở spec sau. None ≠ lỗi.
            logger.info("zalo_parse skip event_name=%s", event_name)
            return None

        # Từ đây là user_send_text — payload SAI ⇒ ValueError (fail loud, không attribute nhầm).
        sender = payload.get("sender")
        message = payload.get("message")
        if not isinstance(sender, Mapping) or not isinstance(message, Mapping):
            raise ValueError("zalo user_send_text missing 'sender'/'message' object")

        external_user_id = sender.get("id")
        text = message.get("text")
        msg_id = message.get("msg_id")
        if not isinstance(external_user_id, str) or not external_user_id:
            raise ValueError("zalo user_send_text missing 'sender.id'")
        if not isinstance(text, str) or not text:
            raise ValueError("zalo user_send_text missing 'message.text'")
        # `msg_id` BẮT BUỘC (spec 17 P3 review HIGH #2): nó là khoá idempotency
        # `webhook_event_log`. Thiếu ⇒ raise (fail loud), KHÔNG default None — None làm webhook
        # bỏ qua dedup ⇒ payload thiếu msg_id (Zalo lỗi / forge) → mỗi retry xử lý lại
        # (amplification). Zalo LUÔN gửi msg_id cho user_send_text (docs 2026-07-24).
        if not isinstance(msg_id, str) or not msg_id:
            raise ValueError("zalo user_send_text missing 'message.msg_id' (idempotency key)")

        return InboundMessage(
            external_user_id=external_user_id,
            text=text,
            external_thread_id=None,  # Zalo user_send_text không có thread; GMF group là event khác
            platform_msg_id=msg_id,
        )

    async def send(self, *, shop_id: str, customer_id: str, text: str) -> None:
        await self._sender.send(shop_id=shop_id, customer_id=customer_id, text=text)
