"""Inbound webhook — channel-agnostic (spec 06 Phase F1; was platform-specific in spec 01).

Route shape is `/webhook/{channel}/{external_id}`: `{channel}` selects an adapter from the
registry the caller passes in, `{external_id}` is the per-shop endpoint id that platform's
gateway was configured with. Nothing in this module knows which platforms exist — adding one
means registering an adapter, not editing request handling (roadmap §5.2.1).

Still NOT mounted in `app/main.py`: `agent/drafter.py::LLMDrafter` shipped in spec 13, so a
concrete `Drafter` exists — the block is customer-inbound safety, not missing code. Mounting
opens the path that reaches the draft engine, which requires Zalo signature-verify + creds
(`GD0-ZALO`, PRE-004, blocked on Tân) and starts the PDPL 60-day clock (workflow §2.5, no
legal owner yet). `enabled=False` is a second, independent guard so even a mounted router
refuses by default until PRE-004 clears.

`shop_id` is DERIVED from `(channel, external_id)` via lookup. The request body is untrusted
and MUST NOT supply a shop_id claim (R1.1 extended) — note the body is handed straight to the
adapter's parser, which only ever reads message content, never tenancy.

When PRE-004 lands: verify the platform signature over the RAW body before parsing.
"""

from __future__ import annotations

import json
from typing import Protocol

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.orchestrator import Drafter, ReceiveOutcome, receive_and_draft
from channels.base import InboundChannel, OutboundChannel
from channels.identity import resolve_conversation
from db.repos import MessageRepo, WebhookEventRepo

# `Drafter` import THẲNG từ `agent.orchestrator` — KHÔNG khai lại ở đây (ISSUE-024).
#
# Module này từng giữ một bản sao `class _Drafter(Protocol)` riêng. Khi spec 10 H2 thêm
# tham số `history` vào `Drafter` thật, bản sao không đổi theo và mypy KHÔNG bắt được: dòng
# đó mang `# type: ignore[no-untyped-def]` (return type untyped ⇒ bỏ qua so khớp). Kết quả
# là một Protocol nói dối — ai viết `Drafter` thật dựa theo nó sẽ qua type-check rồi nổ
# `TypeError` lúc chạy, vì orchestrator gọi kèm `history=`.
#
# Bài học không phải "quên sửa một dòng" mà là: hai bản khai của cùng một khái niệm chỉ
# đồng bộ tới lần đổi kế tiếp. Nguồn sự thật là bên ĐỊNH NGHĨA hành vi (orchestrator gọi
# `draft()`), nên nó giữ Protocol; các module khác import.


class _Channel(InboundChannel, OutboundChannel, Protocol):
    """A channel usable on this route must both parse inbound and send outbound."""


def build_router(
    drafter: Drafter,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    channels: dict[str, _Channel],
    endpoint_to_shop: dict[tuple[str, str], str],
    shop_auto_enabled: dict[str, frozenset[str]],
    enabled: bool = False,
) -> APIRouter:
    """Assemble the inbound router.

    `channels`: channel name → adapter. This mapping is the ONLY place platform names live.
    `endpoint_to_shop`: `(channel, external_id)` → shop_id. Temporary in-memory map; a
    `shops` table lookup lands with Spec 03 Phase 1.
    `shop_auto_enabled`: per-shop opt-in intent sets — an unconfigured shop defaults to an
    empty set, so it always parks rather than auto-sending.
    `enabled=False` returns 503 on every request.
    """

    router = APIRouter(prefix="/webhook", tags=["webhook"])

    @router.post("/{channel}/{external_id}")
    async def inbound(
        channel: str,
        external_id: str,
        req: Request,
    ) -> dict[str, object]:
        # ⚠️ `Body(...)` đã bị GỠ (spec 17 P1): FastAPI parse body TRƯỚC handler chạy, tức
        # payload đã được đọc + parse trước signature verify — mất tính "verify raw bytes".
        # Giờ đọc raw body qua verify, downstream re-parse cùng bytes để đảm bảo consistency.

        if not enabled:
            raise HTTPException(status_code=503, detail="webhook_disabled")

        adapter = channels.get(channel)
        if adapter is None:
            raise HTTPException(status_code=404, detail="unknown_channel")

        shop_id = endpoint_to_shop.get((channel, external_id))
        if shop_id is None:
            # Same shape as "unknown channel" — do not leak which endpoints are registered.
            raise HTTPException(status_code=404, detail="unknown_endpoint")

        # Spec 17 P1: verify signature TRƯỚC parse — chốt chặn duy nhất giữa "webhook mở"
        # và "adapter đọc payload". Verify FAIL ⇒ HTTPException 401/400 bubble lên FastAPI,
        # parse_inbound KHÔNG chạy.
        #
        # Core KHÔNG biết channel dùng scheme gì (Zalo dùng sha256+OA-secret, Messenger sẽ
        # dùng HMAC-SHA1+App-secret, v.v). Verify là method của adapter — Core chỉ hỏi
        # `adapter.verify_signature(...)`.
        #
        # **Fail-loud khi adapter thiếu method** (P1 review HIGH 1): trả 501 chứ KHÔNG skip.
        # Silent skip = adapter mới quên implement ⇒ bypass security control mà không tín
        # hiệu. 501 làm oncall thấy ngay khi enabled=True + first request. Test FakeChannel
        # phải add no-op verify_signature (ok — test-only bypass là intent tường minh).
        verify_fn = getattr(adapter, "verify_signature", None)
        if verify_fn is None:
            raise HTTPException(
                status_code=501,
                detail="channel_verify_not_implemented",
            )
        raw = await verify_fn(req, session_factory)
        payload = json.loads(raw)

        try:
            msg = adapter.parse_inbound(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="unparsable_payload") from exc

        # `None` = event channel CỐ Ý bỏ qua (Zalo image/sticker/oa_send_*/chưa-doc). ACK 200
        # để platform không retry — nhưng KHÔNG xử lý. Khác 400 (payload hỏng) ở chỗ đây là
        # skip hợp lệ, không phải lỗi.
        if msg is None:
            return {"action": "skipped", "reason": "unhandled_event", "reply_id": None}

        # External identity → our identity. This is what removed the orchestrator's old
        # `conversation_id or customer_id` shim: real rows exist before the draft is parked.
        # resolve_conversation commit Customer/Conversation riêng — idempotent (ON CONFLICT
        # + re-select), an toàn commit sớm: retry cùng khách tái dùng row, không đẻ trùng.
        async with session_factory() as session:
            customer_id, conversation_id = await resolve_conversation(
                session,
                shop_id=shop_id,
                channel=channel,
                external_user_id=msg.external_user_id,
                external_thread_id=msg.external_thread_id,
            )

            # Idempotency + append ATOMIC trong MỘT transaction (spec 17 P3 review HIGH fix).
            # Thứ tự & atomicity là chốt chặn silent-message-loss: record_event ghi TRƯỚC
            # append nhưng CHỈ commit CÙNG append (commit=False cả hai). Nếu tách commit —
            # record 'processed' rồi append lỗi → retry thấy duplicate → DROP tin khách vĩnh
            # viễn (Zalo không cho đọc lại). Với atomic: append lỗi ⇒ record cũng rollback ⇒
            # retry reprocess ⇒ tin không mất. record_event=False (duplicate thật) ⇒ rollback
            # + ACK 200: tin đã nằm trong log từ lần commit trước, không xử lý lại.
            #
            # `platform_msg_id is None` = channel KHÔNG cấp khoá idempotency (FakeChannel test;
            # Zalo user_send_text LUÔN có msg_id — parse_inbound raise nếu thiếu, chống
            # amplification). Không có khoá ⇒ append thường (commit=True), không dedup được —
            # thà xử lý 2 lần (duplicate) còn hơn drop. Dedup thật cần khoá platform.
            msg_repo = MessageRepo(session, shop_scope=shop_id)
            if msg.platform_msg_id is not None:
                is_new = await WebhookEventRepo(session).record_event(
                    channel=channel,
                    platform_msg_id=msg.platform_msg_id,
                    shop_id=shop_id,
                    commit=False,
                )
                if not is_new:
                    await session.rollback()
                    return {
                        "action": "duplicate",
                        "reason": "already_processed",
                        "reply_id": None,
                    }
                # Atomic với record_event: cùng transaction, commit chung dưới.
                await msg_repo.append(
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    role="user",
                    content=msg.text,
                    commit=False,
                )
                await session.commit()  # record_event + append: cả hai hoặc không cái nào
            else:
                # Channel không có idempotency key — append thường (không dedup).
                await msg_repo.append(
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    role="user",
                    content=msg.text,
                )

        outcome: ReceiveOutcome = await receive_and_draft(
            shop_id=shop_id,
            customer_id=customer_id,
            conversation_id=conversation_id,
            message=msg.text,
            drafter=drafter,
            sender=adapter,
            session_factory=session_factory,
            shop_auto_enabled_intents=shop_auto_enabled.get(shop_id, frozenset()),
        )
        return {
            "action": outcome.action,
            "reason": outcome.reason,
            "reply_id": outcome.reply_id,
        }

    return router
