"""Zalo webhook signature verification (spec 17 P1, `GD0-ZALO`).

Formula CANONICAL (docs Zalo 2026-07-24):
    X-ZEvent-Signature: mac = sha256(appId + data + timeStamp + OAsecretKey)

Trong đó `data` = raw body bytes y hệt Zalo push — KHÔNG re-serialize JSON (spacing/key
order đổi ⇒ hash lệch), KHÔNG parse trước rồi hash lại. `timeStamp` = `body["timestamp"]`
(string milliseconds), KHÔNG phải header.

**Key lookup — bẫy #2:** Zalo envelope KHÔNG có `oa_id` top-level (chỉ `app_id`). `oa_id`
phải suy từ candidate IDs trong body:
- `user_send_*` events → `recipient.id` = OA (khách gửi TỚI OA)
- `oa_send_*` echo → `sender.id` = OA (OA là người gửi)

Verify thử CẢ 2 candidate qua `oa_secret_lookup`; 1 match ⇒ tính hash với secret đó và
compare. 0 match hoặc hash lệch ⇒ 401. Không lộ candidate nào có trong DB (cùng response
401, không phân biệt "unknown oa_id" với "signature mismatch").

**Replay window ±5 phút.** Zalo push realtime; cửa sổ hẹp chống replay attack (capture
payload, gửi lại 1 tuần sau). `MAX_SKEW_MS` là hằng số module — dời rộng phải qua PR
riêng có lý do.

**`compare_digest` constant-time** — `==` trên chuỗi hex leak byte-prefix match qua timing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

MAX_SKEW_MS = 5 * 60 * 1000  # ±5 phút


async def verify_zalo_signature(
    req: Request,
    oa_secret_lookup: Callable[[str], Awaitable[str | None]],
) -> bytes:
    """Verify header `X-ZEvent-Signature` trên raw body → trả raw bytes; raise 401/400.

    Downstream (`api/webhook.py`) re-parse raw bytes trả về (json.loads) thay vì gọi lại
    `req.body()` — ASGI request body chỉ đọc được 1 lần và ta không muốn cache/rewind ở tầng
    này. Trả raw bytes cũng cho phép log audit sau này (không thuộc P1) mà không phải re-body.

    Raises:
      HTTPException(401): missing header, hash mismatch, replay window, unknown oa_id.
      HTTPException(400): body không parse được JSON hoặc thiếu field bắt buộc.
    """
    raw = await req.body()

    header_sig = req.headers.get("x-zevent-signature")
    if not header_sig:
        logger.info("zalo_sig missing_header")
        raise HTTPException(status_code=401, detail="invalid_signature")

    # Parse body chỉ để lấy `app_id`, `timestamp`, và 2 candidate IDs — KHÔNG dùng cho
    # downstream (downstream re-parse cùng bytes để đảm bảo consistency).
    try:
        body = json.loads(raw)
        app_id = body["app_id"]
        timestamp = body["timestamp"]
        sender_id = body["sender"]["id"]
        recipient_id = body["recipient"]["id"]
    except (KeyError, TypeError, json.JSONDecodeError):
        logger.info("zalo_sig malformed_body")
        raise HTTPException(status_code=400, detail="malformed_body") from None

    # Replay window — timestamp là string unix ms per docs Zalo
    try:
        ts_ms = int(timestamp)
    except (TypeError, ValueError):
        logger.info("zalo_sig malformed_timestamp")
        raise HTTPException(status_code=400, detail="malformed_body") from None

    now_ms = int(time.time() * 1000)
    if abs(now_ms - ts_ms) > MAX_SKEW_MS:
        logger.info("zalo_sig replay skew_ms=%d", now_ms - ts_ms)
        raise HTTPException(status_code=401, detail="invalid_signature")

    # Key lookup: thử cả 2 candidate. Không lộ candidate nào có DB (cùng response).
    # Order: recipient trước (case user_send_* phổ biến hơn), fallback sender.
    #
    # ⚠️ **CONFUSED-DEPUTY GAP đã biết (spec 17 P1 review HIGH 2, defer P4):**
    # Verify hiện KHÔNG bind endpoint↔oa_id. Attacker có oa_secret của shop A + biết
    # endpoint của shop B ⇒ gửi payload với oa_id=A tới endpoint=B ⇒ verify PASS ⇒
    # message attribute nhầm shop. Mitigation: P4 land `shops.zalo_oa_id` (unique) +
    # `endpoint_to_shop` map (channel, external_id) → (shop_id, allowed_oa_ids); verify
    # check candidate ∈ allowed_oa_ids trước khi lookup secret. Hôm nay P4 BLOCKED
    # (PRE-004 chờ Tân), spec 17 P0-P3 vẫn ship — mount là bước ngăn (enabled=False).
    for candidate_oa_id in (recipient_id, sender_id):
        secret = await oa_secret_lookup(candidate_oa_id)
        if secret is None:
            continue
        # Compute hash với secret của candidate này. Nếu match ⇒ verify PASS.
        base = app_id + raw.decode("utf-8") + timestamp + secret
        expected = hashlib.sha256(base.encode("utf-8")).hexdigest()
        if hmac.compare_digest(header_sig, expected):
            return raw

    # DEBUG chứ KHÔNG INFO (spec 17 P1 review MED 3): external IDs từ Zalo là PII per PDPL;
    # log INFO ⇒ vào production log aggregator ⇒ vi phạm data-minimization. Debug đủ để
    # oncall reproduce khi bật log-level DEBUG local, không leak khi mount thật.
    logger.debug("zalo_sig verify_failed sender=%s recipient=%s", sender_id, recipient_id)
    raise HTTPException(status_code=401, detail="invalid_signature")
