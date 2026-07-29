"""Channel abstraction — the seam that lets a new messaging platform be ADDED rather than
cut into the core (spec 06 Phase F1).

Roadmap §1.1.5 / §5.2.1: land this early or pay a 3–5× refactor tax at GĐ2. Before F1,
`api/webhook.py` imported `bridge.zalo_sender` directly and routed `/webhook/zalo/{oa_id}`,
so "add Messenger" meant editing core request handling. After F1 the core knows only these
two Protocols and a `{channel}` path segment.

Scope discipline (§5.2.4 "không generic sớm"): this Protocol is shaped for the ONE channel
that exists (Zalo) plus the one known to be coming (Messenger). It deliberately does NOT
model attachments, reactions, read-receipts, typing indicators or delivery callbacks — we
have not seen two real channels yet, so we do not know which of those generalize. Add them
when a second channel proves the shape, not before.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class InboundMessage:
    """A customer message, normalized away from any one platform's payload shape.

    `external_user_id` is the SENDER's id **as that channel knows it** (a Zalo user id, a
    Messenger PSID). It is deliberately NOT our `customer_id`: mapping external → internal
    identity is `channels.identity.resolve_conversation`'s job, and it is shop-scoped, so
    the same external id under two shops yields two different customers.
    """

    external_user_id: str
    text: str
    external_thread_id: str | None = None
    # `platform_msg_id` (spec 17 P3): id tin nhắn phía platform (Zalo `message.msg_id`). Dùng cho
    # idempotency `webhook_event_log` PK `(channel, platform_msg_id)` — Zalo retry cùng payload ⇒
    # cùng msg_id ⇒ dedup ở tầng DB. `None` khi channel không cấp (không chặn dedup, chỉ bỏ qua).
    platform_msg_id: str | None = None


@runtime_checkable
class InboundChannel(Protocol):
    """Parses a platform webhook payload into `InboundMessage`, or `None` to skip it."""

    name: str

    def parse_inbound(self, payload: Mapping[str, Any]) -> InboundMessage | None:
        """Trả `InboundMessage` cho tin cần xử lý, `None` cho event channel CỐ Ý bỏ qua
        (vd Zalo `user_send_image`/`oa_send_*`/event chưa doc — spec 17 chỉ text). `None` ≠ lỗi:
        caller ACK 200 và không xử lý. Raise `ValueError` khi payload SAI (thiếu sender id /
        text cho event đáng-lẽ-xử-lý) — caller đổi thành 400. Ranh giới: skip có chủ đích →
        None; payload hỏng → raise. KHÔNG bao giờ trả message nửa vời (sender id thiếu = fail
        loud, không attribute nhầm khách)."""
        ...


@runtime_checkable
class OutboundChannel(Protocol):
    """Sends text back to a customer.

    Signature intentionally mirrors `bridge.zalo_sender.ZaloSender.send` so the existing
    sender (and the real one landing in Spec 03c) plugs in unchanged.
    """

    name: str

    async def send(self, *, shop_id: str, customer_id: str, text: str) -> None: ...
