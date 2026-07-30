"""Inbound webhook — channel-agnostic (spec 06 Phase F1; was platform-specific in spec 01).

Route shape is `/webhook/{channel}/{external_id}`: `{channel}` selects an adapter from the
registry the caller passes in, `{external_id}` is the per-shop endpoint id that platform's
gateway was configured with. Nothing in this module knows which platforms exist — adding one
means registering an adapter, not editing request handling (roadmap §5.2.1).

**A5 — đường ACK, không phải đường xử lý.** Trước A5 handler này ghi message + gọi drafter
ĐỒNG BỘ (LLM chạy trong request webhook — platform timeout là mất tin). Giờ nó làm đúng một
việc sau verify + parse: ghi sổ idempotency + enqueue outbox trong MỘT câu (§6.1, I7) rồi
ACK 200. Message + draft là việc của `app/worker_seller.py` (design §3) — process khác,
nhịp khác, chết cũng không mất tin vì `raw_event`/`payload` đã bền trong DB trước khi ACK.

Still NOT mounted in `app/main.py` / `app/main_seller.py`: the block is customer-inbound
safety, not missing code. Mounting opens the path that reaches the draft engine, which
requires Zalo signature-verify + creds (`GD0-ZALO`, PRE-004, blocked on Tân) and starts the
PDPL 60-day clock (workflow §2.5, no legal owner yet). `enabled=False` is a second,
independent guard so even a mounted router refuses by default until PRE-004 clears.

`shop_id` is DERIVED from `(channel, external_id)` via lookup. The request body is untrusted
and MUST NOT supply a shop_id claim (R1.1 extended) — note the body is handed straight to the
adapter's parser, which only ever reads message content, never tenancy.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from channels.base import InboundChannel
from channels.identity import resolve_conversation
from db.repos import OutboxPayload, WebhookEventRepo

# `Drafter` import đã GỠ ở A5 — handler này không draft nữa. Bài học ISSUE-024 (Protocol
# bản sao nói dối) vẫn áp dụng cho mọi seam khác trong file: nguồn sự thật là bên ĐỊNH
# NGHĨA hành vi, các module khác import.


def build_router(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    channels: dict[str, InboundChannel],
    endpoint_to_shop: dict[tuple[str, str], str],
    enabled: bool = False,
) -> APIRouter:
    """Assemble the inbound router.

    `channels`: channel name → adapter. This mapping is the ONLY place platform names live.
    A5: chỉ cần `InboundChannel` (parse + verify) — sender/drafter dọn sang worker, webhook
    không còn đường nào chạm LLM hay outbound.
    `endpoint_to_shop`: `(channel, external_id)` → shop_id. Temporary in-memory map; a
    `shops` table lookup lands with Spec 03 Phase 1.
    `enabled=False` returns 503 on every request.
    """

    router = APIRouter(prefix="/webhook", tags=["webhook"])

    @router.post("/{channel}/{external_id}")
    async def inbound(
        channel: str,
        external_id: str,
        req: Request,
    ) -> JSONResponse:
        # ⚠️ `Body(...)` đã bị GỠ (spec 17 P1): FastAPI parse body TRƯỚC handler chạy, tức
        # payload đã được đọc + parse trước signature verify — mất tính "verify raw bytes".
        # Giờ đọc raw body qua verify, downstream re-parse cùng bytes để đảm bảo consistency.

        # G6: trace sinh Ở DÒNG ĐẦU — mọi response của handler này, KỂ CẢ nhánh lỗi, mang
        # `X-Trace-Id` (review A5-A8: delivery bị 400/422 chính là thứ cần đối chiếu với
        # log retry phía platform nhất, mà trước đây lại là nhánh duy nhất không trace
        # được). Lỗi raise TRONG adapter (verify 401/400) vẫn ngoài tầm — ghi nhận, không
        # vá hộ channel ở đây.
        trace_id = uuid.uuid4()
        trace_header = {"X-Trace-Id": str(trace_id)}

        if not enabled:
            raise HTTPException(status_code=503, detail="webhook_disabled", headers=trace_header)

        adapter = channels.get(channel)
        if adapter is None:
            raise HTTPException(status_code=404, detail="unknown_channel", headers=trace_header)

        shop_id = endpoint_to_shop.get((channel, external_id))
        if shop_id is None:
            # Same shape as "unknown channel" — do not leak which endpoints are registered.
            raise HTTPException(status_code=404, detail="unknown_endpoint", headers=trace_header)

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
                headers=trace_header,
            )
        raw = await verify_fn(req, session_factory)
        payload = json.loads(raw)

        try:
            msg = adapter.parse_inbound(payload)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="unparsable_payload", headers=trace_header
            ) from exc

        # `None` = event channel CỐ Ý bỏ qua (Zalo image/sticker/oa_send_*/chưa-doc). ACK 200
        # để platform không retry — nhưng KHÔNG xử lý. Khác 400 (payload hỏng) ở chỗ đây là
        # skip hợp lệ, không phải lỗi.
        if msg is None:
            return _ack(trace_id, action="skipped", reason="unhandled_event")

        # Đường queue BẮT BUỘC có khoá idempotency (§6.1 — PK `(channel, platform_msg_id)`).
        # Channel không cấp ⇒ 422 fail-loud (quyết 2026-07-30, lượt duyệt A5), KHÔNG âm thầm
        # xử lý không-dedup như trước A5: một channel thật thiếu msg_id là lỗi tích hợp phải
        # thấy ngay, không phải chế độ chạy. Zalo luôn có (`parse_inbound` raise nếu thiếu).
        if msg.platform_msg_id is None:
            raise HTTPException(
                status_code=422, detail="missing_idempotency_key", headers=trace_header
            )

        # Đường rẻ cho retry storm: point-read PK trước khi trả giá resolve (~5 round-trip
        # + commit) — duplicate là ca THƯỜNG GẶP nhất của webhook khi platform retry lúc
        # latency tăng, tức đúng lúc DB đang chậm. Miss vẫn rơi qua §6.1 atomic bên dưới
        # nên race hai bản sao đầu tiên vẫn đúng-một-bên-thắng; đây chỉ là tối ưu.
        async with session_factory() as session:
            if await WebhookEventRepo(session).was_seen(
                channel=channel, platform_msg_id=msg.platform_msg_id
            ):
                return _ack(trace_id, action="duplicate", reason="already_processed")

        # External identity → our identity, TRƯỚC khi enqueue: worker nhờ vậy không cần
        # adapter — payload đã mang id CỦA TA. `resolve_conversation` idempotent (ON CONFLICT
        # + re-select) nên chạy trước dedup không đẻ trùng khi platform retry.
        async with session_factory() as session:
            customer_id, conversation_id = await resolve_conversation(
                session,
                shop_id=shop_id,
                channel=channel,
                external_user_id=msg.external_user_id,
                external_thread_id=msg.external_thread_id,
            )

            # §6.1 — idempotency + enqueue trong MỘT câu lệnh (I7). `None` = platform retry
            # một event đã ghi ⇒ ACK 200 để nó thôi retry, KHÔNG enqueue lại (tin đã nằm
            # trong queue/messages từ lần trước). `raw_event` giữ payload thô để worker
            # re-derive được khi cần; `payload` là bản chuẩn hoá worker tiêu thụ trực tiếp.
            # Shape payload sống ở `OutboxPayload` (ISSUE-024 — một nguồn sự thật với
            # worker), KHÔNG phải dict literal tại chỗ.
            outbox_id = await WebhookEventRepo(session).record_and_enqueue(
                channel=channel,
                platform_msg_id=msg.platform_msg_id,
                shop_id=shop_id,
                raw_event=payload,
                payload=OutboxPayload(
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    channel=channel,
                    platform_msg_id=msg.platform_msg_id,
                    text=msg.text,
                ).to_payload(),
                trace_id=trace_id,
            )

        if outbox_id is None:
            return _ack(trace_id, action="duplicate", reason="already_processed")
        return _ack(trace_id, action="queued", reason="enqueued_for_worker")

    return router


def _ack(trace_id: uuid.UUID, *, action: str, reason: str) -> JSONResponse:
    """ACK 200 thống nhất — mọi nhánh trả cùng shape + `X-Trace-Id` (design §7)."""
    return JSONResponse(
        status_code=200,
        content={"action": action, "reason": reason},
        headers={"X-Trace-Id": str(trace_id)},
    )
