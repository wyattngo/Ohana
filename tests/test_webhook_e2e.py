"""Spec 17 P3 — webhook E2E: signed payload → verify → parse → park (RISK: medium, RED first).

Đường đầy đủ trong test env: POST signed Zalo envelope → verify_signature (P1) → parse_inbound
(P3, real envelope) → resolve_conversation → MessageRepo.append → receive_and_draft (MockDrafter
low-conf → PARK) → PendingReply row. KHÔNG mount app thật (P4 blocked) — build_router + TestClient.

Idempotency: 2 webhook cùng `msg_id` → chỉ 1 `messages` row + 1 `pending_replies` row (qua
`webhook_event_log` PK `(channel, platform_msg_id)`). Đây là DB-level idempotency (spec 14 B0
đã dựng bảng), P3 wire `record_event` vào webhook path — KHÔNG phải queue/ACK (đó là GD0-INGEST).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from api.webhook import build_router
from bridge.zalo_sender import MockZaloSender
from channels.zalo import ZaloChannel
from db.models import Conversation, Customer, Message, PendingReply, Shop
from db.repos import ZaloOATokenRepo

_APP_ID = "2074138120372622546"
_OA_ID = "2074138120372622547"
_USER_ID = "3742389367648617405"
_OA_SECRET = "e2e-oa-secret-per-oa"


def _sig(app_id: str, raw: bytes, ts: str, secret: str) -> str:
    return hashlib.sha256((app_id + raw.decode("utf-8") + ts + secret).encode("utf-8")).hexdigest()


def _envelope(text: str = "còn hàng ko", msg_id: str = "e2e-msg-1") -> tuple[bytes, str]:
    ts = str(int(datetime.now(UTC).timestamp() * 1000))
    payload = {
        "app_id": _APP_ID,
        "sender": {"id": _USER_ID},
        "recipient": {"id": _OA_ID},
        "event_name": "user_send_text",
        "message": {"text": text, "msg_id": msg_id},
        "timestamp": ts,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8"), ts


@dataclass
class _D:
    text: str
    intent: str
    confidence: float


class _LowConfDrafter:
    async def draft(self, *, shop_id, customer_id, message, history) -> _D:
        return _D(text="draft trả lời", intent="general_qa", confidence=0.2)  # → park


async def _seed(session_factory) -> None:
    now = datetime.now(UTC)
    async with session_factory() as s:
        s.add(Shop(id="shop_a", name="Shop A"))
        await s.commit()
        await ZaloOATokenRepo(s).update_tokens_locked(
            shop_id="shop_a",
            oa_id=_OA_ID,
            access_token="a",
            refresh_token="r",
            access_expires_at=now + timedelta(hours=1),
            refresh_expires_at=now + timedelta(days=90),
            oa_secret_key=_OA_SECRET,
        )


def _client(session_factory) -> TestClient:
    router = build_router(
        _LowConfDrafter(),
        session_factory,
        channels={"zalo": ZaloChannel(sender=MockZaloSender())},  # type: ignore[dict-item]
        endpoint_to_shop={("zalo", "EP1"): "shop_a"},
        shop_auto_enabled={},
        enabled=True,
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


async def _count(session_factory, model) -> int:
    async with session_factory() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.mark.asyncio
async def test_e2e_signed_payload_parks_reply(fresh_db):
    """Full path: signed real envelope → verify → parse → park. PendingReply row landed."""
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    client = _client(session_factory)

    raw, ts = _envelope()
    resp = client.post(
        "/webhook/zalo/EP1",
        content=raw,
        headers={"X-ZEvent-Signature": _sig(_APP_ID, raw, ts, _OA_SECRET)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "park"

    # Identity thật + tin khách ghi + draft park
    assert await _count(session_factory, Customer) == 1
    assert await _count(session_factory, Conversation) == 1
    assert await _count(session_factory, Message) == 1  # tin khách
    assert await _count(session_factory, PendingReply) == 1


@pytest.mark.asyncio
async def test_e2e_duplicate_msg_id_idempotent(fresh_db):
    """2 webhook cùng msg_id → chỉ 1 messages row + 1 pending_replies (webhook_event_log dedup)."""
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    client = _client(session_factory)

    raw, ts = _envelope(msg_id="dup-msg")
    headers = {"X-ZEvent-Signature": _sig(_APP_ID, raw, ts, _OA_SECRET)}

    r1 = client.post("/webhook/zalo/EP1", content=raw, headers=headers)
    r2 = client.post("/webhook/zalo/EP1", content=raw, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200, "retry cùng msg_id vẫn ACK 200 (không lỗi)"

    # Idempotent: KHÔNG nhân đôi row
    assert await _count(session_factory, Message) == 1, "duplicate msg_id không được tạo 2 messages"
    assert await _count(session_factory, PendingReply) == 1


@pytest.mark.asyncio
async def test_e2e_append_failure_does_not_lose_message_on_retry(fresh_db, monkeypatch):
    """P3 review HIGH #1: record_event + append ATOMIC. Nếu append lỗi SAU record_event, retry
    KHÔNG được coi là duplicate → tin khách reprocess, KHÔNG mất vĩnh viễn.

    Mô phỏng: lần POST đầu, `MessageRepo.append` throw (DB blip). Vì atomic, record_event
    rollback cùng → webhook_event_log KHÔNG có row. Lần POST thứ hai (Zalo retry cùng msg_id)
    → record_event thấy NEW (không phải duplicate) → append thành công → tin nằm trong log.
    Không có atomic thì lần 2 thấy duplicate → DROP → mất tin.
    """
    import db.repos as repos_module

    _, session_factory = await fresh_db()
    await _seed(session_factory)
    # raise_server_exceptions=False: append throw → 500 response thay vì propagate lên test
    router = build_router(
        _LowConfDrafter(),
        session_factory,
        channels={"zalo": ZaloChannel(sender=MockZaloSender())},  # type: ignore[dict-item]
        endpoint_to_shop={("zalo", "EP1"): "shop_a"},
        shop_auto_enabled={},
        enabled=True,
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    raw, ts = _envelope(msg_id="flaky-msg")
    headers = {"X-ZEvent-Signature": _sig(_APP_ID, raw, ts, _OA_SECRET)}

    real_append = repos_module.MessageRepo.append
    calls = {"n": 0}

    async def flaky_append(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated DB blip during append")
        return await real_append(self, **kwargs)

    monkeypatch.setattr(repos_module.MessageRepo, "append", flaky_append)

    # Lần 1: append throw → 500 (không nuốt lỗi). record_event PHẢI rollback cùng.
    r1 = client.post("/webhook/zalo/EP1", content=raw, headers=headers)
    assert r1.status_code == 500

    # webhook_event_log KHÔNG được có row 'flaky-msg' (đã rollback cùng append)
    from db.models import WebhookEventLog

    assert await _count(session_factory, WebhookEventLog) == 0, (
        "record_event phải rollback cùng append lỗi — nếu không retry sẽ bị coi duplicate"
    )

    # Lần 2 (Zalo retry): append lần này OK → tin khách reprocess, KHÔNG mất
    r2 = client.post("/webhook/zalo/EP1", content=raw, headers=headers)
    assert r2.status_code == 200, r2.text
    assert await _count(session_factory, Message) == 1, "tin khách phải recover ở retry, không mất"


@pytest.mark.asyncio
async def test_e2e_bad_signature_401_no_processing(fresh_db):
    """Verify fail → 401, KHÔNG parse, KHÔNG ghi row nào (chốt chặn P1 giữ trên đường E2E)."""
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    client = _client(session_factory)

    raw, _ = _envelope()
    resp = client.post(
        "/webhook/zalo/EP1",
        content=raw,
        headers={"X-ZEvent-Signature": "f" * 64},
    )
    assert resp.status_code == 401
    assert await _count(session_factory, Customer) == 0
    assert await _count(session_factory, Message) == 0
    assert await _count(session_factory, PendingReply) == 0
